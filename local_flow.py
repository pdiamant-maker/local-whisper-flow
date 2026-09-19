"""
Local Flow: a lightweight Windows dictation helper.

Hotkey:
    Ctrl + Shift + J toggles recording on/off.

Install dependencies:
    pip install faster-whisper sounddevice soundfile pynput pyperclip pyautogui numpy

Optional tray icon:
    pip install pystray pillow

Optional target-app icon in the overlay:
    pip install pywin32

Optional Parakeet STT engine (STT_ENGINE = "parakeet", the default):
    pip install onnx-asr[gpu,hub] onnxruntime-gpu==1.22.0 nvidia-cuda-runtime-cu12 nvidia-cufft-cu12

Overlay:
    A dark rounded panel at the bottom center of the screen showing the target
    app icon, the live (partial) transcript while you speak, and the animated
    waveform bars with the current status.

Notes:
    - Ollama must already be running at OLLAMA_URL.
    - The selected Ollama model must already be pulled locally.
    - faster-whisper with device="cuda" requires a working NVIDIA CUDA runtime.
    - Parakeet (onnx-asr) needs the nvidia CUDA runtime wheels on PATH; see
      configure_cuda_dll_search().
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import numpy as np
import pyautogui
import pyperclip
import sounddevice as sd
import soundfile as sf
from pynput import keyboard

if TYPE_CHECKING:
    from faster_whisper import WhisperModel as FasterWhisperModel

try:
    import pystray
    from PIL import Image, ImageDraw

    TRAY_IMPORTS_AVAILABLE = True
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None
    TRAY_IMPORTS_AVAILABLE = False


# =============================================================================
# EDIT THESE — user configuration
# Every knob you would normally change lives in this section. Defaults run on
# CPU with no GPU required. See README.md for what each setting does.
# =============================================================================

APP_NAME = "Local Flow"
HOTKEY = "<ctrl>+<shift>+j"

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:7b-instruct"  # Higher polish. Use "qwen2.5:3b-instruct" for lower latency.
OLLAMA_TIMEOUT_SECONDS = 90
OLLAMA_KEEP_ALIVE = "30m"
ENABLE_OLLAMA_REFINEMENT = True
OLLAMA_NUM_PREDICT = 96

# Speech-to-text engine:
#   "parakeet" -> NVIDIA Parakeet via onnx-asr. Fast, punctuated output, needs the GPU wheels.
#   "whisper"  -> faster-whisper fallback, works on CPU.
STT_ENGINE = "parakeet"
PARAKEET_MODEL_NAME = "nemo-parakeet-tdt-0.6b-v3"

# Whisper model. Smaller = faster, less accurate. Good options:
#   "base.en" / "small.en"  -> best for CPU (low latency), the fallback if no GPU
#   "large-v3-turbo"        -> best accuracy, needs a GPU to stay fast
WHISPER_MODEL_NAME = "large-v3-turbo"

# Device: "cpu" works everywhere (fallback: WHISPER_MODEL_NAME="base.en", WHISPER_COMPUTE_TYPE="int8").
# "cuda" requires an NVIDIA GPU with the CUDA runtime installed (see the GPU section of the README).
WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE_TYPE = "float16"  # CPU users: set WHISPER_DEVICE="cpu" and this to "int8".

# ADVANCED / GPU ONLY: extra folders to search for CUDA runtime DLLs (cuBLAS/cuDNN).
# Leave empty on CPU. On Windows GPU setups, add the folder(s) containing
# cublas64_12.dll / cudnn*.dll if they are not already on your PATH, e.g.:
#   EXTRA_CUDA_DLL_DIRS = [r"C:\path\to\cuda\bin"]
# All nvidia wheel bin dirs in the venv: onnxruntime's CUDA provider needs cuda_runtime
# and cufft on top of the cublas/cudnn that faster-whisper wants.
EXTRA_CUDA_DLL_DIRS: list[str] = sorted(
    str(p)
    for p in (Path(__file__).resolve().parent / ".venv" / "Lib" / "site-packages" / "nvidia").glob("*/bin")
)

SAMPLE_RATE = 16000
CHANNELS = 1
PASTE_DELAY_SECONDS = 0.08
ENABLE_TRAY_ICON = True
ENABLE_FLOATING_OVERLAY = True
FLOATING_OVERLAY_SHOW_WHEN_IDLE = False
STARTUP_READY_OVERLAY_SECONDS = 4
OVERLAY_IDLE_LINGER_SECONDS = 1.0  # How long the panel stays after a dictation finishes.
LIVE_TRANSCRIBE_INTERVAL_SECONDS = 0.25 if STT_ENGINE == "parakeet" else 1.0  # How often the live transcript on the overlay refreshes.
LIVE_TRANSCRIBE_WINDOW_SECONDS = 15.0  # The overlay only shows the tail, so a fixed window keeps latency flat on long dictations.
OVERLAY_PANEL_WIDTH = 480
OVERLAY_PANEL_HEIGHT = 150
OVERLAY_BOTTOM_MARGIN = 80
TRANSCRIPTION_LANGUAGE = "en"  # Skips language detection for faster English dictation.
HIDE_CONSOLE_ON_START = os.environ.get("LOCAL_FLOW_DEBUG_CONSOLE") != "1"
SHOW_METRICS = True  # Show a live "total 0.7s · whisper 0.3 · llm 0.1" line on the overlay after each dictation.
ENABLE_LIVE_PREVIEW = True  # Show the streaming partial transcript on the overlay.
OVERLAY_SCALE = 1.0  # Multiplier applied to overlay panel size.
ACTIVATION_MODE = "toggle"  # "toggle" (press to start/stop) or "hold" (record while held).
PASTE_LAST_HOTKEY = ""  # Optional hotkey to re-paste the last transcript. Empty = disabled.

ENABLE_HISTORY = True  # Record each dictation to history.db for the History tab.
SAVE_AUDIO = False  # Also keep a copy of the WAV next to history.db (privacy-sensitive, off by default).
AUDIO_CAP_MB = 1024  # Oldest saved WAVs are pruned once the audio/ folder exceeds this size.
WEEKENDS_DONT_BREAK_STREAK = True  # Sat/Sun gaps don't end a dictation streak, for Insights.

KEEP_ON_CLIPBOARD = False  # Skip restoring the previous clipboard contents after paste.
LOWERCASE_FIRST = False  # Lowercase the first character of each dictation.
STRIP_TRAILING_PERIOD = False  # Remove a single trailing "." from each dictation.
SPACE_BETWEEN_DICTATIONS = False  # Prefix a space when the previous dictation didn't end in whitespace.
DISPLAY_NAME = "Petros Diamant"  # Shown in the sidebar profile card.
INPUT_DEVICE = ""  # sounddevice input device name. Empty = system default.

APP_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = Path(__file__).resolve()
LOG_FILE = APP_DIR / "local_flow.log"
SHORTCUT_NAME = f"{APP_NAME}.lnk"
SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"
HISTORY_DB = APP_DIR / "history.db"
DICTIONARY_PATH = APP_DIR / "dictionary.json"
HF_CACHE_DIR = Path.home() / ".cache" / "huggingface" / "hub"


def _hf_cache_dirs(patterns: list[str]) -> list[Path]:
    """Best-effort: HF cache subdirs matching any of the given glob patterns."""
    if not HF_CACHE_DIR.exists():
        return []
    dirs = []
    for pattern in patterns:
        dirs.extend(d for d in HF_CACHE_DIR.glob(pattern) if d.is_dir())
    return dirs


def _hf_cache_dir_size(patterns: list[str]) -> Optional[int]:
    """Total size in bytes of matching HF cache dirs, or None if none found/unreadable."""
    dirs = _hf_cache_dirs(patterns)
    if not dirs:
        return None
    total = 0
    try:
        for d in dirs:
            total += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    except OSError:
        return None
    return total

SETTINGS_KEYS = {
    "hotkey": "HOTKEY",
    "stt_engine": "STT_ENGINE",
    "parakeet_model": "PARAKEET_MODEL_NAME",
    "whisper_model": "WHISPER_MODEL_NAME",
    "whisper_device": "WHISPER_DEVICE",
    "whisper_compute_type": "WHISPER_COMPUTE_TYPE",
    "ollama_model": "OLLAMA_MODEL",
    "ollama_refinement": "ENABLE_OLLAMA_REFINEMENT",
    "tray_icon": "ENABLE_TRAY_ICON",
    "floating_overlay": "ENABLE_FLOATING_OVERLAY",
    "live_preview": "ENABLE_LIVE_PREVIEW",
    "live_interval_seconds": "LIVE_TRANSCRIBE_INTERVAL_SECONDS",
    "overlay_scale": "OVERLAY_SCALE",
    "activation_mode": "ACTIVATION_MODE",
    "paste_last_hotkey": "PASTE_LAST_HOTKEY",
    "save_history": "ENABLE_HISTORY",
    "save_audio": "SAVE_AUDIO",
    "audio_cap_mb": "AUDIO_CAP_MB",
    "weekends_streak": "WEEKENDS_DONT_BREAK_STREAK",
    "copy_to_clipboard": "KEEP_ON_CLIPBOARD",
    "lowercase_first": "LOWERCASE_FIRST",
    "strip_trailing_period": "STRIP_TRAILING_PERIOD",
    "space_between": "SPACE_BETWEEN_DICTATIONS",
    "display_name": "DISPLAY_NAME",
    "input_device": "INPUT_DEVICE",
}

# Settings keys that only take effect after a restart (surfaced to the UI as a chip).
RESTART_REQUIRED_KEYS = {
    "stt_engine", "whisper_model", "whisper_device", "whisper_compute_type",
    "parakeet_model", "hotkey", "activation_mode", "paste_last_hotkey",
    "overlay_scale", "tray_icon", "floating_overlay",
}

_settings_lock = threading.Lock()


def _settings_snapshot() -> dict:
    """Current values of all whitelisted globals, keyed by their JSON key."""
    return {json_key: globals()[target] for json_key, target in SETTINGS_KEYS.items()}


def _rebind_live_interval_if_default(explicit_keys: set[str]) -> None:
    # Keep the parakeet 0.25 / whisper 1.0 default rule, unless the file set it explicitly.
    if "live_interval_seconds" not in explicit_keys:
        globals()["LIVE_TRANSCRIBE_INTERVAL_SECONDS"] = 0.25 if STT_ENGINE == "parakeet" else 1.0


def load_settings() -> None:
    with _settings_lock:
        if not SETTINGS_PATH.exists():
            try:
                SETTINGS_PATH.write_text(json.dumps(_settings_snapshot(), indent=2), encoding="utf-8")
            except OSError as exc:
                print(f"[settings] could not create {SETTINGS_PATH}: {exc}")
            return

        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[settings] could not read {SETTINGS_PATH}: {exc}; using defaults")
            return

        explicit_keys: set[str] = set()
        for json_key, target in SETTINGS_KEYS.items():
            if json_key not in data:
                continue
            value = data[json_key]
            default = globals()[target]
            if type(value) is not type(default):
                print(f"[settings] ignoring {json_key!r}: expected {type(default).__name__}, got {type(value).__name__}")
                continue
            globals()[target] = value
            explicit_keys.add(json_key)

        _rebind_live_interval_if_default(explicit_keys)


def save_settings(updates: dict) -> None:
    with _settings_lock:
        try:
            current = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}

        current.update({k: v for k, v in updates.items() if k in SETTINGS_KEYS})
        SETTINGS_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")

        explicit_keys: set[str] = set()
        for json_key, target in SETTINGS_KEYS.items():
            if json_key in current:
                globals()[target] = current[json_key]
                explicit_keys.add(json_key)
        _rebind_live_interval_if_default(explicit_keys)


SYSTEM_PROMPT = """You clean raw speech-to-text dictation.

Rules:
- Remove filler words, false starts, repeated stutters, and verbal hesitation.
- Add correct punctuation, capitalization, and paragraph breaks when useful.
- Keep every remaining word exactly as the speaker said it. Never substitute, reorder, or add words.
- Do not fix grammar or word choice. Awkward phrasing stays as spoken.
- Keep technical terms, names, numbers, and code-like text intact.
- Execute spoken formatting commands instead of transcribing them: "new line" -> line break, "new paragraph" -> blank line, "make this a list" / "as a list" -> format the items as a dash list. Only apply when clearly spoken as a command.
- Output ONLY the clean final statement.
- Do not add commentary, explanations, quotes, markdown, prefixes, or suffixes."""


# -----------------------------
# Runtime state
# -----------------------------

audio_queue: "queue.Queue[np.ndarray]" = queue.Queue()
overlay_queue: "queue.Queue[str]" = queue.Queue()
recording_lock = threading.Lock()
is_recording = False
is_processing = False
input_stream: Optional[sd.InputStream] = None
recorded_chunks: list[np.ndarray] = []
whisper_model: Optional["FasterWhisperModel"] = None
parakeet_model = None
model_load_lock = threading.Lock()
listener: Optional[keyboard.Listener] = None
hotkey: Optional[keyboard.HotKey] = None  # toggle-mode combo
hold_hotkey: Optional["HoldHotkey"] = None  # hold-mode combo
paste_last_hotkey: Optional[keyboard.HotKey] = None
tray_icon = None
shutdown_event = threading.Event()
current_status = "starting"
show_idle_overlay_until = 0.0
last_metrics_text = ""  # Populated after each dictation when SHOW_METRICS is on.
last_final_text = ""  # The most recent pasted/clean transcript, shown in the main window's Last Result card.
_prev_dictation_had_trailing_space = True  # Tracks SPACE_BETWEEN_DICTATIONS state across dictations.
live_partial_text = ""  # ponytail: plain str, assignment is atomic; a stale frame costs nothing.
webview_window = None  # The pywebview main window, created hidden on the main thread at startup.
_dll_directory_handles = []
_app_icon_cache: dict[str, object] = {}  # exe path -> PIL Image (or None when unavailable)
_icon_warning_logged = False

TRAY_COLORS = {
    "starting": (73, 144, 226),
    "idle": (45, 180, 92),
    "recording": (220, 67, 67),
    "processing": (245, 166, 35),
    "error": (130, 130, 130),
}

OVERLAY_COLORS = {
    "starting": "#498FE2",
    "idle": "#2DB45C",
    "recording": "#DC4343",
    "processing": "#F5A623",
    "error": "#828282",
}

OVERLAY_LABELS = {
    "starting": "START",
    "idle": "READY",
    "recording": "REC",
    "processing": "BUSY",
    "error": "ERR",
}


def log(message: str) -> None:
    """Print a timestamped terminal message."""
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    try:
        print(line, flush=True)
    except OSError:
        pass

    try:
        with LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")
    except OSError:
        pass


def hide_console_window() -> None:
    """Hide the console window when launched with python.exe instead of pythonw.exe."""
    if not HIDE_CONSOLE_ON_START or os.name != "nt":
        return

    try:
        import ctypes

        console_window = ctypes.windll.kernel32.GetConsoleWindow()
        if console_window:
            ctypes.windll.user32.ShowWindow(console_window, 0)
    except Exception:
        pass


def powershell_quote(value: str) -> str:
    """Quote a Python string for PowerShell single-quoted literals."""
    return "'" + value.replace("'", "''") + "'"


def get_windows_folder(name: str, fallback: Path) -> Path:
    """Get Windows special folders such as Desktop through .NET."""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"[Environment]::GetFolderPath({powershell_quote(name)})",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        value = result.stdout.strip()
        if value:
            return Path(value)
    except Exception:
        pass

    return fallback


def get_desktop_shortcut_path() -> Path:
    return get_windows_folder("Desktop", Path.home() / "Desktop") / SHORTCUT_NAME


def get_startup_shortcut_path() -> Path:
    startup_dir = (
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
    return startup_dir / SHORTCUT_NAME


def get_windowless_python() -> Path:
    """Prefer pythonw.exe so shortcut launches do not leave a console window open."""
    python_exe = Path(sys.executable).resolve()
    pythonw_exe = python_exe.with_name("pythonw.exe")
    if pythonw_exe.exists():
        return pythonw_exe
    return python_exe


def create_windows_shortcut(shortcut_path: Path, *, startup: bool = False) -> None:
    """Create a Windows .lnk shortcut to this script."""
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    target_path = get_windowless_python()
    launcher_path = APP_DIR / "local_flow_launcher.pyw"
    launch_script = launcher_path if launcher_path.exists() else SCRIPT_PATH
    arguments = f'"{launch_script}"'
    description = f"{APP_NAME} local dictation"
    icon_path = APP_DIR / "local_flow.ico"
    icon_location = f"{icon_path},0" if icon_path.exists() else f"{target_path},0"
    window_style = 7 if startup else 1

    script = f"""
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut({powershell_quote(str(shortcut_path))})
$shortcut.TargetPath = {powershell_quote(str(target_path))}
$shortcut.Arguments = {powershell_quote(arguments)}
$shortcut.WorkingDirectory = {powershell_quote(str(APP_DIR))}
$shortcut.Description = {powershell_quote(description)}
$shortcut.IconLocation = {powershell_quote(icon_location)}
$shortcut.WindowStyle = {window_style}
$shortcut.Save()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )


def create_desktop_shortcut() -> Path:
    shortcut_path = get_desktop_shortcut_path()
    create_windows_shortcut(shortcut_path)
    log(f"Desktop shortcut created: {shortcut_path}")
    return shortcut_path


def enable_start_with_windows() -> Path:
    shortcut_path = get_startup_shortcut_path()
    create_windows_shortcut(shortcut_path, startup=True)
    log(f"Start with Windows enabled: {shortcut_path}")
    return shortcut_path


def disable_start_with_windows() -> None:
    shortcut_path = get_startup_shortcut_path()
    if shortcut_path.exists():
        shortcut_path.unlink()
        log("Start with Windows disabled.")
    else:
        log("Start with Windows was already disabled.")


def open_path(path: Path) -> None:
    try:
        os.startfile(str(path))
    except OSError as exc:
        log(f"Could not open {path}: {exc}")


def configure_cuda_dll_search() -> None:
    """Expose known CUDA runtime folders to this Python process."""
    # Parakeet always wants the DLLs on PATH; whisper only when it runs on GPU.
    if STT_ENGINE == "whisper" and WHISPER_DEVICE.lower() != "cuda":
        return

    for raw_dir in EXTRA_CUDA_DLL_DIRS:
        cuda_dir = Path(raw_dir)
        if not cuda_dir.exists():
            continue

        try:
            _dll_directory_handles.append(os.add_dll_directory(str(cuda_dir)))
        except (AttributeError, OSError) as exc:
            log(f"Could not add CUDA DLL directory {cuda_dir}: {exc}")

        path_parts = os.environ.get("PATH", "").split(os.pathsep)
        if str(cuda_dir) not in path_parts:
            os.environ["PATH"] = str(cuda_dir) + os.pathsep + os.environ.get("PATH", "")

        log(f"Using CUDA DLL directory: {cuda_dir}")


_TRAY_ENERGY = {
    "recording": 1.0,
    "processing": 0.55,
    "starting": 0.35,
    "idle": 0.18,
    "error": 0.10,
}


def make_tray_image(status: str, phase: float = 0.0):
    """Create a tiny animated equalizer tray icon for the current status.

    `phase` advances over time to animate the bars; call with a rising value.
    """
    if not TRAY_IMPORTS_AVAILABLE:
        return None

    color = TRAY_COLORS.get(status, TRAY_COLORS["idle"])
    energy = _TRAY_ENERGY.get(status, _TRAY_ENERGY["idle"])
    speed = 9.0 if status == "recording" else 2.5

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    mid = 32
    for i, x in enumerate((14, 26, 38, 50)):
        wave = 0.5 + 0.5 * math.sin(phase * speed + i * 1.9)
        half = max(4, int(24 * energy * (0.35 + 0.65 * wave)))
        draw.line((x, mid - half, x, mid + half), fill=color, width=8)
    return image


def set_status(status: str) -> None:
    """Update terminal/tray status."""
    global current_status, show_idle_overlay_until

    current_status = status
    if status == "idle":
        # First idle after launch gets the long READY grace; later idles close fast.
        linger = STARTUP_READY_OVERLAY_SECONDS if show_idle_overlay_until == 0.0 else OVERLAY_IDLE_LINGER_SECONDS
        show_idle_overlay_until = time.time() + linger
    overlay_queue.put(status)
    if tray_icon is not None and TRAY_IMPORTS_AVAILABLE:
        tray_icon.icon = make_tray_image(status)
        tray_icon.title = f"Local Flow - {status}"


def get_foreground_exe_path() -> Optional[str]:
    """Return the executable path of the current foreground window, or None."""
    if os.name != "nt":
        return None

    try:
        import ctypes
        import ctypes.wintypes as wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        window = user32.GetForegroundWindow()
        if not window:
            return None

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if not pid.value:
            return None

        process = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return None

        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                return None
            return buffer.value
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        return None


def get_foreground_window_title() -> str:
    """Return the title of the current foreground window, best-effort empty string on failure."""
    if os.name != "nt":
        return ""

    try:
        user32 = ctypes.windll.user32
        window = user32.GetForegroundWindow()
        if not window:
            return ""
        length = user32.GetWindowTextLengthW(window)
        if not length:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, buffer, length + 1)
        return buffer.value
    except Exception:
        return ""


# -----------------------------
# Dictation history (SQLite, private, off by default in the shipped log level)
# -----------------------------

_history_lock = threading.Lock()
_history_conn: Optional[sqlite3.Connection] = None

_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS dictations (
    id INTEGER PRIMARY KEY,
    ts REAL,
    app TEXT,
    window_title TEXT,
    raw TEXT,
    final TEXT,
    chars INTEGER,
    ai_processed INTEGER,
    model TEXT,
    audio_secs REAL,
    audio_path TEXT,
    whisper_secs REAL,
    llm_secs REAL
)
"""


def _get_history_conn() -> sqlite3.Connection:
    """Module-level SQLite connection, created on first use. Caller holds _history_lock."""
    global _history_conn
    if _history_conn is None:
        _history_conn = sqlite3.connect(str(HISTORY_DB), check_same_thread=False)
        _history_conn.execute("PRAGMA journal_mode=WAL")
        _history_conn.execute(_HISTORY_SCHEMA)
        _history_conn.commit()
    return _history_conn


def _prune_audio_dir(audio_dir: Path) -> None:
    """Delete oldest WAVs until the folder is back under AUDIO_CAP_MB."""
    try:
        files = sorted(audio_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        cap_bytes = AUDIO_CAP_MB * 1024 * 1024
        for f in files:
            if total <= cap_bytes:
                break
            total -= f.stat().st_size
            f.unlink(missing_ok=True)
    except Exception as exc:
        log(f"Audio pruning failed (non-fatal): {exc}")


def record_history(
    raw_text: str,
    clean_text: str,
    transcribe_seconds: float,
    ollama_seconds: float,
    audio_seconds: float,
    wav_path: Optional[Path],
) -> None:
    """Save one dictation to history.db. Never raises into the dictation flow."""
    if not ENABLE_HISTORY:
        return

    try:
        ts = time.time()
        exe_path = get_foreground_exe_path()
        app = Path(exe_path).stem if exe_path else ""
        window_title = get_foreground_window_title()
        model = OLLAMA_MODEL if ENABLE_OLLAMA_REFINEMENT else (
            PARAKEET_MODEL_NAME if STT_ENGINE == "parakeet" else WHISPER_MODEL_NAME
        )

        audio_path = ""
        if SAVE_AUDIO and wav_path and wav_path.exists():
            audio_dir = APP_DIR / "audio"
            audio_dir.mkdir(exist_ok=True)
            dest = audio_dir / f"{int(ts * 1000)}.wav"
            shutil.copyfile(wav_path, dest)
            audio_path = str(dest)
            _prune_audio_dir(audio_dir)

        with _history_lock:
            conn = _get_history_conn()
            conn.execute(
                "INSERT INTO dictations "
                "(ts, app, window_title, raw, final, chars, ai_processed, model, audio_secs, audio_path, whisper_secs, llm_secs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts, app, window_title, raw_text, clean_text, len(clean_text),
                    1 if ENABLE_OLLAMA_REFINEMENT else 0, model, audio_seconds,
                    audio_path or None, transcribe_seconds, ollama_seconds,
                ),
            )
            conn.commit()
    except Exception as exc:
        log(f"History recording failed (non-fatal): {exc}")


def history_list_rows(query: str, limit: int) -> list[dict]:
    try:
        with _history_lock:
            conn = _get_history_conn()
            like = f"%{query}%"
            if query:
                cursor = conn.execute(
                    "SELECT id, ts, app, final, ai_processed, audio_path FROM dictations "
                    "WHERE raw LIKE ? OR final LIKE ? ORDER BY ts DESC LIMIT ?",
                    (like, like, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT id, ts, app, final, ai_processed, audio_path FROM dictations "
                    "ORDER BY ts DESC LIMIT ?",
                    (limit,),
                )
            rows = cursor.fetchall()
        return [
            {
                "id": row_id,
                "ts": ts,
                "app": app or "",
                "snippet": (final or "")[:90],
                "ai": bool(ai_processed),
                "has_audio": bool(audio_path),
            }
            for row_id, ts, app, final, ai_processed, audio_path in rows
        ]
    except Exception as exc:
        log(f"history_list failed: {exc}")
        return []


def history_get_row(row_id: int) -> Optional[dict]:
    try:
        with _history_lock:
            conn = _get_history_conn()
            cursor = conn.execute("SELECT * FROM dictations WHERE id = ?", (row_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row))
    except Exception as exc:
        log(f"history_get failed: {exc}")
        return None


def history_delete_row(row_id: int) -> bool:
    try:
        with _history_lock:
            conn = _get_history_conn()
            conn.execute("DELETE FROM dictations WHERE id = ?", (row_id,))
            conn.commit()
        return True
    except Exception as exc:
        log(f"history_delete failed: {exc}")
        return False


def history_clear_rows() -> bool:
    try:
        with _history_lock:
            conn = _get_history_conn()
            conn.execute("DELETE FROM dictations")
            conn.commit()
        return True
    except Exception as exc:
        log(f"history_clear failed: {exc}")
        return False


def _word_count(text: Optional[str]) -> int:
    return len(text.split()) if text else 0


def _format_hour_label(hour: int) -> str:
    def fmt(h: int) -> str:
        h12 = h % 12 or 12
        return f"{h12}{'am' if h < 12 else 'pm'}"

    return f"{fmt(hour)}–{fmt((hour + 1) % 24)}"


def _streak_gap_bridgeable(prev_date: date, cur_date: date, weekends_dont_break: bool) -> bool:
    """True if `cur_date` continues an active streak that last ran on `prev_date`."""
    gap_days = (cur_date - prev_date).days
    if gap_days <= 1:
        return True
    if not weekends_dont_break:
        return False
    day = prev_date + timedelta(days=1)
    while day < cur_date:
        if day.weekday() not in (5, 6):  # Saturday, Sunday
            return False
        day += timedelta(days=1)
    return True


def _compute_streaks(active_dates: list[date], today: date, weekends_dont_break: bool) -> tuple[int, int]:
    """Returns (longest_streak, current_streak) in days, from sorted unique active dates."""
    if not active_dates:
        return 0, 0

    longest = run = 1
    for i in range(1, len(active_dates)):
        if _streak_gap_bridgeable(active_dates[i - 1], active_dates[i], weekends_dont_break):
            run += 1
        else:
            longest = max(longest, run)
            run = 1
    longest = max(longest, run)

    last_active = active_dates[-1]
    current = run if _streak_gap_bridgeable(last_active, today, weekends_dont_break) else 0
    return longest, current


def compute_insights(conn: sqlite3.Connection) -> dict:
    """Pure function over a dictations connection so it is unit-testable without history.db."""
    rows = conn.execute("SELECT ts, final, ai_processed, app FROM dictations").fetchall()
    if not rows:
        return {"empty": True}

    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())

    total_words = 0
    words_today = 0
    words_this_week = 0
    max_words_single = 0
    ai_count = 0
    words_by_day: dict[date, int] = {}
    count_by_day: dict[date, int] = {}
    hour_counts: dict[int, int] = {}
    app_counts: dict[str, int] = {}

    for ts, final, ai_processed, app in rows:
        wc = _word_count(final)
        total_words += wc
        max_words_single = max(max_words_single, wc)
        if ai_processed:
            ai_count += 1

        dt = datetime.fromtimestamp(ts)
        d = dt.date()
        words_by_day[d] = words_by_day.get(d, 0) + wc
        count_by_day[d] = count_by_day.get(d, 0) + 1
        hour_counts[dt.hour] = hour_counts.get(dt.hour, 0) + 1
        if app:
            app_counts[app] = app_counts.get(app, 0) + 1
        if d == today:
            words_today += wc
        if d >= week_start:
            words_this_week += wc

    total_count = len(rows)
    fav_hour = max(hour_counts.items(), key=lambda kv: kv[1])[0] if hour_counts else None
    active_dates = sorted(words_by_day.keys())
    longest_streak, current_streak = _compute_streaks(active_dates, today, WEEKENDS_DONT_BREAK_STREAK)

    heatmap_start = today - timedelta(weeks=16)
    heatmap = [
        {"date": d.isoformat(), "words": w}
        for d, w in sorted(words_by_day.items())
        if d >= heatmap_start
    ]
    per_app = sorted(app_counts.items(), key=lambda kv: -kv[1])[:7]

    return {
        "empty": False,
        "total_words": total_words,
        "total_count": total_count,
        "avg_words": round(total_words / total_count, 1) if total_count else 0,
        "pct_ai": round(ai_count / total_count * 100, 1) if total_count else 0,
        "hours_saved": round(total_words * (1 / 40 - 1 / 150) / 60, 1),
        "speed_multiple": 3.8,  # ponytail: static 150wpm dictation vs 40wpm typing, not measured live.
        "words_this_week": words_this_week,
        "words_today": words_today,
        "max_words_single": max_words_single,
        "max_words_day": max(words_by_day.values()) if words_by_day else 0,
        "max_dictations_day": max(count_by_day.values()) if count_by_day else 0,
        "favorite_hour_range": _format_hour_label(fav_hour) if fav_hour is not None else "—",
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "heatmap": heatmap,
        "per_app": [{"app": app, "count": count} for app, count in per_app],
    }


def insights_compute() -> dict:
    """Insights over the real history.db. Never raises into the UI."""
    try:
        with _history_lock:
            return compute_insights(_get_history_conn())
    except Exception as exc:
        log(f"insights_compute failed: {exc}")
        return {"empty": True}


# -----------------------------
# Custom dictionary (applied to raw transcript before AI cleanup)
# -----------------------------

_dictionary_lock = threading.Lock()
_dictionary_cache: Optional[list] = None


def load_dictionary() -> list[dict]:
    """[{"hears": ["variant", ...], "writes": "Correct"}], cached until a mutation reloads it."""
    global _dictionary_cache
    with _dictionary_lock:
        if _dictionary_cache is not None:
            return _dictionary_cache
        if not DICTIONARY_PATH.exists():
            _dictionary_cache = []
            return _dictionary_cache
        try:
            data = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
            _dictionary_cache = data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            log(f"[dictionary] could not read {DICTIONARY_PATH}: {exc}")
            _dictionary_cache = []
        return _dictionary_cache


def _save_dictionary(entries: list[dict]) -> None:
    global _dictionary_cache
    with _dictionary_lock:
        DICTIONARY_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        _dictionary_cache = entries


def apply_dictionary(text: str) -> str:
    """Case-insensitive, whole-word(ish) replacement of each configured variant."""
    if not text:
        return text
    for entry in load_dictionary():
        writes = entry.get("writes") or ""
        if not writes:
            continue
        for variant in entry.get("hears") or []:
            if not variant:
                continue
            text = re.sub(r"\b" + re.escape(variant) + r"\b", writes, text, flags=re.IGNORECASE)
    return text


def get_app_icon_image(exe_path: str):
    """Extract a 24x24 PIL image of an exe's icon. Returns None when unavailable."""
    global _icon_warning_logged

    if exe_path in _app_icon_cache:
        return _app_icon_cache[exe_path]

    image = None
    try:
        try:
            import win32gui
            import win32ui
            from PIL import Image as PILImage
        except ImportError:
            if not _icon_warning_logged:
                _icon_warning_logged = True
                log("Target icon disabled. Install optional dependency: pip install pywin32")
            _app_icon_cache[exe_path] = None
            return None

        large, small = win32gui.ExtractIconEx(exe_path, 0)
        handles = list(large) + list(small)
        if not handles:
            _app_icon_cache[exe_path] = None
            return None

        icon_handle = handles[0]
        screen_dc = memory_dc = bitmap = None
        try:
            screen_dc = win32ui.CreateDCFromHandle(win32gui.GetDC(0))
            memory_dc = screen_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(screen_dc, 32, 32)
            memory_dc.SelectObject(bitmap)
            # DrawIcon uses the system icon size (40/48px at >100% display scaling), which crops
            # into a 32x32 bitmap. DrawIconEx draws at an explicit size instead.
            ctypes.windll.user32.DrawIconEx(
                memory_dc.GetSafeHdc(), 0, 0, int(icon_handle), 32, 32, 0, None, 0x0003  # DI_NORMAL
            )
            raw = bitmap.GetBitmapBits(True)
            image = PILImage.frombuffer("RGBA", (32, 32), raw, "raw", "BGRA", 0, 1)
            if image.getchannel("A").getextrema() == (0, 0):
                image.putalpha(255)  # Some icons render without alpha; keep them visible.
            image = image.resize((24, 24), PILImage.LANCZOS)
        finally:
            for handle in handles:
                try:
                    win32gui.DestroyIcon(handle)
                except Exception:
                    pass
            try:
                if bitmap is not None:
                    win32gui.DeleteObject(bitmap.GetHandle())
                if memory_dc is not None:
                    memory_dc.DeleteDC()
                if screen_dc is not None:
                    screen_dc.DeleteDC()
            except Exception:
                pass
    except Exception as exc:
        log(f"Could not read target app icon: {exc}")
        image = None

    _app_icon_cache[exe_path] = image
    return image


def draw_round_rect(canvas, x1, y1, x2, y2, radius, **options) -> None:
    """Draw a rounded rectangle on a tkinter canvas."""
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    canvas.create_polygon(points, smooth=True, **options)


def start_floating_overlay() -> None:
    """Start an always-on-top desktop indicator."""
    if not ENABLE_FLOATING_OVERLAY:
        return

    def run_overlay() -> None:
        try:
            import tkinter as tk
        except ImportError:
            log("Floating overlay disabled because Tkinter is not available.")
            return

        root = tk.Tk()
        root.title("Local Flow")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        root.configure(bg="#101820")
        root.attributes("-alpha", 1.0)
        try:
            # Windows: paint the box color as fully transparent so only the bars/text show.
            root.attributes("-transparentcolor", "#101820")
        except tk.TclError:
            pass

        from tkinter import font as tkfont

        scale = OVERLAY_SCALE if OVERLAY_SCALE > 0 else 1.0

        def fsize(n: int) -> int:
            return max(6, round(n * scale))

        width = round(OVERLAY_PANEL_WIDTH * scale)
        height = round(OVERLAY_PANEL_HEIGHT * scale)
        canvas = tk.Canvas(root, width=width, height=height, bg="#101820", highlightthickness=0)
        canvas.pack()

        text_font = tkfont.Font(family="Segoe UI", size=fsize(10))
        text_left = round(76 * scale)
        text_width = width - text_left - round(20 * scale)
        icon_photos: dict[str, object] = {}  # Keep PhotoImage refs alive.
        current_icon_path: Optional[str] = None
        next_icon_poll = 0.0

        def place_window() -> None:
            screen_width = root.winfo_screenwidth()
            screen_height = root.winfo_screenheight()
            x = max(0, (screen_width - width) // 2)
            y = max(0, screen_height - height - OVERLAY_BOTTOM_MARGIN)
            root.geometry(f"{width}x{height}+{x}+{y}")

        def wrap_tail(text: str, max_lines: int = 3) -> str:
            """Wrap text to the panel width and keep only the last `max_lines` lines."""
            lines: list[str] = []
            line = ""
            for word in text.split():
                candidate = f"{line} {word}".strip()
                if line and text_font.measure(candidate) > text_width:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            if line:
                lines.append(line)
            return "\n".join(lines[-max_lines:])

        def refresh_target_icon() -> None:
            """Cache a PhotoImage of the foreground app's icon (tkinter thread only)."""
            nonlocal current_icon_path

            exe_path = get_foreground_exe_path()
            if not exe_path or exe_path == sys.executable:
                return  # Our own overlay/settings window: keep whatever icon we had.

            if exe_path == current_icon_path:
                return

            if exe_path not in icon_photos:
                image = get_app_icon_image(exe_path)
                photo = None
                if image is not None:
                    try:
                        from PIL import ImageTk

                        photo = ImageTk.PhotoImage(image)
                    except Exception as exc:
                        log(f"Could not build target icon image: {exc}")
                icon_photos[exe_path] = photo

            if icon_photos[exe_path] is not None:
                current_icon_path = exe_path

        # Equalizer geometry and per-status motion energy (eased for smooth transitions).
        bar_center = width // 2
        bar_xs = tuple(bar_center + round(offset * scale) for offset in (-18, -9, 0, 9, 18))
        bar_mid = round(112 * scale)
        bar_max_half = round(16 * scale)
        energy_targets = {"recording": 1.0, "processing": 0.55, "starting": 0.35, "idle": 0.16, "error": 0.1}
        energy = energy_targets["idle"]

        def mic_level() -> float:
            """Rough RMS of the newest pending mic frame (peek only, no dequeue)."""
            try:
                chunk = audio_queue.queue[-1]
                return float(np.sqrt(np.mean(np.square(chunk))))
            except (IndexError, ValueError):
                return 0.0

        def render(status: str) -> None:
            nonlocal energy

            should_hide_idle = (
                status == "idle"
                and not FLOATING_OVERLAY_SHOW_WHEN_IDLE
                and time.time() >= show_idle_overlay_until
            )
            if should_hide_idle:
                root.withdraw()
                return

            root.deiconify()
            place_window()
            color = OVERLAY_COLORS.get(status, OVERLAY_COLORS["idle"])
            label = OVERLAY_LABELS.get(status, status.upper())

            # Ease toward the status energy so recording->idle settles over ~0.5s.
            energy += (energy_targets.get(status, energy_targets["idle"]) - energy) * 0.12

            now = time.time()
            live = min(1.0, mic_level() * 8.0) if status == "recording" else 0.0

            canvas.delete("all")
            canvas.create_rectangle(0, 0, width, height, fill="#101820", outline="")
            draw_round_rect(canvas, 1, 1, width - 1, height - 1, (height - 2) // 2, fill="#0D1117", outline="#2A3340", width=1)

            icon_x, icon_y, icon_size = round(44 * scale), round(20 * scale), round(24 * scale)
            photo = icon_photos.get(current_icon_path) if current_icon_path else None
            if photo is not None:
                canvas.create_image(icon_x, icon_y, image=photo, anchor="nw")
            else:
                canvas.create_oval(icon_x, icon_y, icon_x + icon_size, icon_y + icon_size, fill="#2A3340", outline="")

            partial = live_partial_text
            if partial:
                canvas.create_text(
                    text_left, round(18 * scale), text=wrap_tail(partial), anchor="nw", justify="left",
                    fill="#E6EDF3" if status == "recording" else "#7A8896",
                    font=("Segoe UI", fsize(10)),
                )
            elif status == "recording":
                canvas.create_text(
                    text_left, round(18 * scale), text="Listening…", anchor="nw",
                    fill="#7A8896", font=("Segoe UI", fsize(10), "italic"),
                )

            canvas.create_text(round(48 * scale), bar_mid, text=label, anchor="w", fill=color, font=("Segoe UI", fsize(9), "bold"))
            for i, x in enumerate(bar_xs):
                if status == "processing":
                    # Gentle pulse traveling across the bars.
                    pos = (now * 2.5) % len(bar_xs)
                    dist = min(abs(i - pos), len(bar_xs) - abs(i - pos))
                    wave = max(0.25, 1.0 - dist * 0.45)
                else:
                    speed = 9.0 if status == "recording" else 2.5
                    wave = 0.5 + 0.5 * math.sin(now * speed + i * 1.9)
                    if live > wave:
                        wave = live  # bars jump with real voice when louder than the idle dance
                half = max(3.0, bar_max_half * energy * (0.35 + 0.65 * wave))
                canvas.create_line(x, bar_mid - half, x, bar_mid + half, fill=color, width=max(2, round(6 * scale)), capstyle=tk.ROUND)
            if SHOW_METRICS and last_metrics_text and status in ("idle", "processing"):
                canvas.create_text(
                    width // 2, height - round(16 * scale),
                    text=last_metrics_text, fill="#7A8896", font=("Segoe UI", fsize(7)),
                )

        def poll() -> None:
            nonlocal next_icon_poll

            now = time.time()
            if now >= next_icon_poll:
                next_icon_poll = now + 0.5
                refresh_target_icon()

            status = current_status
            while True:
                try:
                    status = overlay_queue.get_nowait()
                except queue.Empty:
                    break

            render(status)

            if shutdown_event.is_set():
                root.destroy()
                return

            root.after(33, poll)

        canvas.bind("<Button-1>", lambda event: toggle_recording())
        render(current_status)
        root.after(33, poll)
        root.mainloop()

    threading.Thread(target=run_overlay, daemon=True).start()


def quit_app(icon=None, item=None) -> None:
    """Stop the app from the tray menu or terminal cleanup."""
    shutdown_event.set()
    if is_recording:
        stop_recording()
    if listener is not None:
        listener.stop()
    if tray_icon is not None:
        tray_icon.stop()
    if webview_window is not None:
        try:
            webview_window.destroy()
        except Exception:
            pass


class UiApi:
    """js_api exposed to ui/index.html: thin wrappers around existing app functions."""

    def get_health(self) -> list[dict]:
        rows = []

        if STT_ENGINE == "parakeet":
            model_ok = parakeet_model is not None
            model_name = PARAKEET_MODEL_NAME
        else:
            model_ok = whisper_model is not None
            model_name = WHISPER_MODEL_NAME
        rows.append({
            "title": "Voice model ready",
            "subtitle": model_name if model_ok else f"{model_name} (still loading)",
            "ok": model_ok,
            "badge": "Ready" if model_ok else "Loading",
        })

        try:
            device_info = sd.query_devices(kind="input")
            mic_name = device_info["name"]
            rows.append({"title": "Microphone access", "subtitle": mic_name, "ok": True, "badge": "Ready"})
        except Exception as exc:
            rows.append({
                "title": "Microphone access",
                "subtitle": f"No input device found ({exc})",
                "ok": False,
                "badge": "Warning",
            })

        rows.append({
            "title": "Speech engine",
            "subtitle": f"Runs on your computer · {STT_ENGINE}",
            "ok": True,
            "badge": "Ready",
        })

        ollama_ok = False
        try:
            tags_url = OLLAMA_URL.rsplit("/", 1)[0] + "/tags"
            with urllib.request.urlopen(tags_url, timeout=2) as response:
                ollama_ok = response.status == 200
        except Exception:
            ollama_ok = False
        rows.append({
            "title": "Cleanup model",
            "subtitle": f"{OLLAMA_MODEL}" if ollama_ok else f"Ollama unreachable ({OLLAMA_MODEL})",
            "ok": ollama_ok,
            "badge": "Ready" if ollama_ok else "Warning",
        })

        rows.append({
            "title": "Hotkey active",
            "subtitle": HOTKEY,
            "ok": True,
            "badge": "Ready",
        })

        return rows

    def get_engines(self) -> list[dict]:
        parakeet_dirs = _hf_cache_dirs(["*parakeet*"])
        whisper_dirs = _hf_cache_dirs(["*whisper-large-v3-turbo*", "*large-v3-turbo*"])
        return [
            {
                "key": "parakeet",
                "name": "Parakeet TDT 0.6B v3",
                "vendor": "NVIDIA",
                "desc": "Fastest transcription, built-in punctuation. 25 European languages.",
                "perf_note": "≈ 0.03 s per pass on this PC · WER 6.3% (Open ASR)",
                "size": _hf_cache_dir_size(["*parakeet*"]),
                "active": STT_ENGINE == "parakeet",
                "downloaded": bool(parakeet_dirs),
            },
            {
                "key": "whisper",
                "name": "Whisper large-v3-turbo",
                "vendor": "OpenAI",
                "desc": "More robust on accents and noise. 100 languages. Slower.",
                "perf_note": "≈ 0.25 s per pass on this PC · WER ~7.5%",
                "size": _hf_cache_dir_size(["*whisper-large-v3-turbo*", "*large-v3-turbo*"]),
                "active": STT_ENGINE == "whisper",
                "downloaded": bool(whisper_dirs),
            },
        ]

    def get_ollama_models(self) -> list[dict]:
        try:
            tags_url = OLLAMA_URL.rsplit("/", 1)[0] + "/tags"
            with urllib.request.urlopen(tags_url, timeout=2) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            log(f"get_ollama_models: Ollama unreachable ({exc})")
            return []

        return [
            {
                "name": model.get("name", ""),
                "size_gb": round(model.get("size", 0) / (1024 ** 3), 1),
                "active": model.get("name") == OLLAMA_MODEL,
            }
            for model in data.get("models", [])
        ]

    def get_status(self) -> dict:
        return {"current_status": current_status, "last_final_text": last_final_text}

    def toggle_recording(self) -> None:
        toggle_recording()

    def copy_last(self) -> bool:
        try:
            pyperclip.copy(last_final_text)
            return True
        except Exception as exc:
            log(f"Could not copy last result: {exc}")
            return False

    def paste_last(self) -> None:
        paste_text(last_final_text)

    def open_settings_placeholder(self) -> str:
        return "later phase"

    def history_list(self, query: str = "", limit: int = 200) -> list[dict]:
        return history_list_rows(query or "", limit)

    def history_get(self, id: int) -> Optional[dict]:
        return history_get_row(id)

    def history_delete(self, id: int) -> bool:
        return history_delete_row(id)

    def history_clear(self) -> bool:
        return history_clear_rows()

    def history_paste(self, id: int) -> None:
        row = history_get_row(id)
        if row:
            paste_text(row.get("final") or "")

    # ---- Settings (Phase 3) ----

    def get_settings(self) -> dict:
        snapshot = _settings_snapshot()
        snapshot["restart_required"] = False
        return snapshot

    def set_settings(self, updates: dict) -> dict:
        updates = updates or {}
        save_settings(updates)
        snapshot = _settings_snapshot()
        snapshot["restart_required"] = any(key in RESTART_REQUIRED_KEYS for key in updates)
        return snapshot

    def start_hotkey_capture(self) -> dict:
        global hotkey_capture_active, _hotkey_capture_state, _hotkey_capture_result
        global _hotkey_capture_live, _hotkey_capture_listener, _hotkey_capture_timer, _hotkey_capture_done

        if _hotkey_capture_listener is not None:
            self.cancel_hotkey_capture()

        _hotkey_capture_down.clear()
        _hotkey_capture_seen.clear()
        _hotkey_capture_done = False
        with _hotkey_capture_lock:
            _hotkey_capture_state = "recording"
            _hotkey_capture_result = None
            _hotkey_capture_live = []
        hotkey_capture_active = True

        _hotkey_capture_listener = keyboard.Listener(on_press=_capture_on_press, on_release=_capture_on_release)
        _hotkey_capture_timer = threading.Timer(15.0, lambda: _capture_finish(None, "cancelled"))
        _hotkey_capture_timer.daemon = True
        _hotkey_capture_listener.start()
        _hotkey_capture_timer.start()
        return self.get_hotkey_capture()

    def get_hotkey_capture(self) -> dict:
        with _hotkey_capture_lock:
            state = _hotkey_capture_state
            combo = _hotkey_capture_result
            live = list(_hotkey_capture_live)
        display = _format_hotkey_display(combo) if combo else (_format_hotkey_display(_finalize_combo(live)) if live else None)
        return {"state": state, "hotkey": combo, "display": display}

    def cancel_hotkey_capture(self) -> None:
        _capture_finish(None, "cancelled")

    def get_launch_at_login(self) -> bool:
        return get_startup_shortcut_path().exists()

    def set_launch_at_login(self, enabled: bool) -> bool:
        try:
            if enabled:
                enable_start_with_windows()
            else:
                disable_start_with_windows()
            return True
        except Exception as exc:
            log(f"set_launch_at_login failed: {exc}")
            return False

    def list_input_devices(self) -> list[dict]:
        try:
            devices = sd.query_devices()
            default_index = sd.default.device[0]
        except Exception as exc:
            log(f"list_input_devices failed: {exc}")
            return []

        seen_names: set = set()
        rows = []
        for index, device in enumerate(devices):
            if device.get("max_input_channels", 0) <= 0:
                continue
            name = device["name"]
            if name in seen_names:
                continue
            seen_names.add(name)
            rows.append({"index": index, "name": name, "default": index == default_index})
        return rows

    def open_url(self, url: str) -> None:
        try:
            webbrowser.open(url)
        except Exception as exc:
            log(f"open_url failed: {exc}")

    def open_logs(self) -> None:
        open_path(LOG_FILE)

    def open_app_folder(self) -> None:
        open_path(APP_DIR)

    def create_desktop_launcher(self) -> dict:
        try:
            path = create_desktop_shortcut()
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            log(f"create_desktop_launcher failed: {exc}")
            return {"ok": False, "error": str(exc)}

    def export_support_bundle(self) -> bool:
        try:
            desktop = get_windows_folder("Desktop", Path.home() / "Desktop")
            zip_path = desktop / "localflow-support.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
                if LOG_FILE.exists():
                    bundle.write(LOG_FILE, LOG_FILE.name)
                if SETTINGS_PATH.exists():
                    bundle.write(SETTINGS_PATH, SETTINGS_PATH.name)
            open_path(desktop)
            return True
        except Exception as exc:
            log(f"export_support_bundle failed: {exc}")
            return False

    def restart_app(self) -> None:
        try:
            subprocess.Popen([str(get_windowless_python()), str(SCRIPT_PATH)], cwd=str(APP_DIR))
        except Exception as exc:
            log(f"restart_app failed: {exc}")
        os._exit(0)

    # ---- Insights (Phase 5) ----

    def get_insights(self) -> dict:
        return insights_compute()

    # ---- Custom dictionary (Phase 6) ----

    def dict_list(self) -> list[dict]:
        return load_dictionary()

    def dict_add(self, hears_csv: str, writes: str) -> dict:
        writes = (writes or "").strip()
        variants = [v.strip() for v in (hears_csv or "").split(",") if v.strip()]
        if not writes or not variants:
            return {"error": "Enter at least one phrase Local Flow hears, and what to change it to."}
        entries = load_dictionary()
        entries.append({"hears": variants, "writes": writes})
        _save_dictionary(entries)
        return {"ok": True}

    def dict_delete(self, index: int) -> bool:
        entries = load_dictionary()
        if 0 <= index < len(entries):
            entries.pop(index)
            _save_dictionary(entries)
            return True
        return False

    def dict_export(self) -> dict:
        try:
            desktop = get_windows_folder("Desktop", Path.home() / "Desktop")
            dest = desktop / "localflow-dictionary.json"
            dest.write_text(json.dumps(load_dictionary(), indent=2), encoding="utf-8")
            open_path(desktop)
            return {"ok": True, "path": str(dest)}
        except Exception as exc:
            log(f"dict_export failed: {exc}")
            return {"error": str(exc)}

    def dict_import(self) -> dict:
        """Reads localflow-dictionary.json from the Desktop and merges it in."""
        try:
            desktop = get_windows_folder("Desktop", Path.home() / "Desktop")
            src = desktop / "localflow-dictionary.json"
            if not src.exists():
                return {"error": f"No file found at {src}. Export a dictionary there first."}
            data = json.loads(src.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return {"error": "That file is not a valid dictionary export."}
            new_entries = [e for e in data if isinstance(e, dict) and e.get("hears") and e.get("writes")]
            entries = load_dictionary() + new_entries
            _save_dictionary(entries)
            return {"ok": True, "count": len(new_entries)}
        except Exception as exc:
            log(f"dict_import failed: {exc}")
            return {"error": str(exc)}


_window_icon_set = False


def set_main_window_icon() -> None:
    """Replace the inherited pythonw.exe title-bar icon with local_flow.ico.

    The native handle only exists after the first show, so this runs from
    open_main_window, slightly delayed.
    """
    global _window_icon_set
    if _window_icon_set:
        return
    try:
        import win32con
        import win32gui

        icon_path = APP_DIR / "local_flow.ico"
        # The hidden tk overlay root is also titled "Local Flow";
        # match the WinForms class to get the webview window.
        hwnds = []

        def collect(h, _):
            if win32gui.GetWindowText(h) == "Local Flow" and win32gui.GetClassName(h).startswith("WindowsForms"):
                hwnds.append(h)

        win32gui.EnumWindows(collect, None)
        if not hwnds or not icon_path.exists():
            return
        for size, kind in ((16, win32con.ICON_SMALL), (32, win32con.ICON_BIG)):
            hicon = win32gui.LoadImage(
                0, str(icon_path), win32con.IMAGE_ICON,
                size, size, win32con.LR_LOADFROMFILE,
            )
            win32gui.SendMessage(hwnds[0], win32con.WM_SETICON, kind, hicon)
        _window_icon_set = True
    except Exception as exc:
        log(f"Could not set window icon: {exc}")


def open_main_window() -> None:
    """Show the main window created on the main thread at startup."""
    if webview_window is None:
        log("Main window is not ready yet.")
        return
    try:
        webview_window.show()
        webview_window.restore()
        threading.Timer(0.5, set_main_window_icon).start()
    except Exception as exc:
        log(f"Could not show the main window: {exc}")


def start_tray_icon() -> None:
    """Start an optional Windows tray icon."""
    global tray_icon

    if not ENABLE_TRAY_ICON:
        return

    if not TRAY_IMPORTS_AVAILABLE:
        log("Tray icon disabled. Install optional dependencies: pip install pystray pillow")
        return

    menu = pystray.Menu(
        pystray.MenuItem("Open Local Flow", lambda icon, item: open_main_window()),
        pystray.MenuItem("Toggle recording", lambda icon, item: toggle_recording()),
        pystray.MenuItem("Quit Local Flow", quit_app),
    )
    tray_icon = pystray.Icon("Local Flow", make_tray_image(current_status), "Local Flow", menu)
    thread = threading.Thread(target=tray_icon.run, daemon=True)
    thread.start()

    def animate_tray() -> None:
        phase = 0.0
        while not shutdown_event.is_set():
            if tray_icon is not None:
                try:
                    tray_icon.icon = make_tray_image(current_status, phase)
                except Exception:
                    pass
            phase += 0.15
            time.sleep(0.12)

    threading.Thread(target=animate_tray, daemon=True).start()


def load_parakeet_model():
    """Load the Parakeet ONNX model once so hotkey dictation stays snappy."""
    global parakeet_model

    with model_load_lock:
        if parakeet_model is not None:
            return parakeet_model

        configure_cuda_dll_search()
        try:
            import onnx_asr
        except ImportError as exc:
            set_status("error")
            log("Could not import onnx_asr.")
            log("Install it with: pip install onnx-asr[gpu,hub]")
            raise RuntimeError(f"Parakeet initialization failed: {exc}") from exc

        log(f"Loading Parakeet '{PARAKEET_MODEL_NAME}' (CUDA)...")
        started = time.perf_counter()
        try:
            parakeet_model = onnx_asr.load_model(
                PARAKEET_MODEL_NAME,
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            )
        except Exception as exc:
            set_status("error")
            log("Could not initialize Parakeet.")
            log("Debug hints:")
            log("- pip install onnx-asr[gpu,hub] onnxruntime-gpu==1.22.0")
            log("- pip install nvidia-cuda-runtime-cu12 nvidia-cufft-cu12")
            log("- Fallback: set STT_ENGINE = 'whisper'.")
            raise RuntimeError(f"Parakeet initialization failed: {exc}") from exc

        log(f"Parakeet model loaded ({time.perf_counter() - started:.2f}s).")
        set_status("idle")
        return parakeet_model


def load_whisper_model() -> "FasterWhisperModel":
    """Load faster-whisper once so hotkey dictation stays snappy."""
    global whisper_model

    if whisper_model is not None:
        return whisper_model

    configure_cuda_dll_search()
    from faster_whisper import WhisperModel

    log(
        "Loading faster-whisper "
        f"'{WHISPER_MODEL_NAME}' on {WHISPER_DEVICE} ({WHISPER_COMPUTE_TYPE})..."
    )
    try:
        whisper_model = WhisperModel(
            WHISPER_MODEL_NAME,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
    except Exception as exc:
        set_status("error")
        log("Could not initialize faster-whisper.")
        log("Debug hints:")
        log("- CPU mode: use WHISPER_DEVICE = 'cpu' and WHISPER_COMPUTE_TYPE = 'int8'.")
        log("- GPU mode: install CUDA/cuDNN DLLs, then use WHISPER_DEVICE = 'cuda'.")
        log("- If this model is unavailable, try WHISPER_MODEL_NAME = 'base.en'.")
        raise RuntimeError(f"Whisper initialization failed: {exc}") from exc

    log("Whisper model loaded.")
    set_status("idle")
    return whisper_model


def audio_callback(indata: np.ndarray, frames: int, callback_time, status) -> None:
    """Receive microphone frames from sounddevice."""
    if status:
        log(f"Audio warning: {status}")
    audio_queue.put(indata.copy())


def drain_audio_queue() -> None:
    """Move pending callback audio into the recording buffer."""
    while True:
        try:
            recorded_chunks.append(audio_queue.get_nowait())
        except queue.Empty:
            break


def start_recording() -> None:
    """Start microphone capture."""
    global input_stream, is_recording, live_partial_text

    with recording_lock:
        if is_recording:
            return

        if is_processing:
            log("Still processing the previous dictation. Please wait a moment.")
            return

        recorded_chunks.clear()
        drain_audio_queue()
        live_partial_text = ""

        try:
            input_stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=audio_callback,
                device=INPUT_DEVICE or None,
            )
            input_stream.start()
        except Exception as exc:
            log("Could not start microphone recording.")
            log("Debug hints:")
            log("- Check Windows microphone permissions.")
            log("- Confirm another app is not exclusively holding the mic.")
            log("- Try setting a specific sounddevice input device.")
            log(f"Raw error: {exc}")
            input_stream = None
            return

        is_recording = True
        set_status("recording")
        threading.Thread(target=live_transcribe_loop, daemon=True).start()
        log("RECORDING ON  | Speak now. Press Ctrl+Shift+J again to stop.")


def stop_recording() -> None:
    """Stop capture and process the recording in a worker thread."""
    global input_stream, is_recording, is_processing

    with recording_lock:
        if not is_recording:
            return

        try:
            if input_stream is not None:
                input_stream.stop()
                input_stream.close()
        except Exception as exc:
            log(f"Audio stream close warning: {exc}")
        finally:
            input_stream = None

        drain_audio_queue()
        is_recording = False

        if not recorded_chunks:
            log("No audio captured.")
            return

        is_processing = True
        set_status("processing")
        log("RECORDING OFF | Transcribing and polishing...")

    worker = threading.Thread(target=process_recording, daemon=True)
    worker.start()


def write_temp_wav() -> tuple[Path, float]:
    """Save recorded chunks to a temporary WAV file in the local directory."""
    audio = np.concatenate(recorded_chunks, axis=0)

    if audio.ndim == 1:
        audio = audio.reshape(-1, 1)

    duration = len(audio) / SAMPLE_RATE
    if duration < 0.25:
        raise ValueError("Recording was too short to transcribe.")

    temp_name = f"local_flow_{int(time.time())}.wav"
    wav_path = Path.cwd() / temp_name
    sf.write(wav_path, audio, SAMPLE_RATE, subtype="PCM_16")
    return wav_path, duration


def transcribe_audio(source, quiet: bool = False) -> str:
    """Transcribe a WAV path or a float32 numpy array with the configured STT engine."""
    if STT_ENGINE == "parakeet":
        model = load_parakeet_model()
        try:
            if isinstance(source, Path):
                audio, sample_rate = sf.read(str(source), dtype="float32")
            else:
                audio, sample_rate = source, SAMPLE_RATE
            text = str(model.recognize(audio, sample_rate=sample_rate)).strip()
        except Exception as exc:
            log(f"Parakeet transcription failed: {exc}")
            raise
        if not quiet:
            log("Transcription complete (parakeet).")
        return text  # ponytail: parakeet already punctuates and capitalizes.

    model = load_whisper_model()
    try:
        segments, info = model.transcribe(
            str(source) if isinstance(source, Path) else source,
            language=TRANSCRIPTION_LANGUAGE,
            beam_size=1,
            best_of=1,
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 350,
                "speech_pad_ms": 200,
            },
            condition_on_previous_text=False,
            temperature=0.0,
            without_timestamps=True,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
    except Exception as exc:
        message = str(exc)
        if "cublas64_12.dll" in message or "cudnn" in message.lower():
            log("CUDA runtime libraries are missing from Windows PATH.")
            log("GPU transcription needs cuBLAS for CUDA 12 and cuDNN 9 for CUDA 12.")
            log("Install those NVIDIA runtime libraries, add their bin folder to PATH,")
            log("then close and reopen this terminal before running the script again.")
            log("Temporary fallback: set WHISPER_DEVICE = 'cpu' and WHISPER_COMPUTE_TYPE = 'int8'.")
        raise

    if not quiet:
        log(
            "Transcription complete "
            f"(language={info.language}, probability={info.language_probability:.2f})."
        )
    return text


def live_transcribe_loop() -> None:
    """Re-transcribe the audio captured so far while recording, for the overlay."""
    global live_partial_text

    if not ENABLE_LIVE_PREVIEW:
        return

    while is_recording and not shutdown_event.is_set():
        time.sleep(LIVE_TRANSCRIBE_INTERVAL_SECONDS)
        if not is_recording or shutdown_event.is_set():
            return

        try:
            # Snapshot without dequeuing: stop_recording() still owns the real drain.
            chunks = list(recorded_chunks) + list(audio_queue.queue)
            if not chunks:
                continue

            audio = np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32)
            audio = audio[-int(SAMPLE_RATE * LIVE_TRANSCRIBE_WINDOW_SECONDS):]
            if len(audio) < SAMPLE_RATE * 0.5:
                continue

            text = transcribe_audio(audio, quiet=True)
            if is_recording and text:
                live_partial_text = text
        except Exception as exc:
            log(f"Live transcription skipped: {exc}")


def refine_with_ollama(raw_text: str) -> str:
    """Send raw transcript to Ollama and return polished text."""
    if not raw_text:
        return ""

    if not ENABLE_OLLAMA_REFINEMENT:
        return raw_text

    prompt = f"""{SYSTEM_PROMPT}

Raw dictation:
{raw_text}

Clean final statement:"""

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {
            "temperature": 0.1,
            "top_p": 0.9,
            "num_predict": OLLAMA_NUM_PREDICT,
        },
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            result = json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        log(f"Ollama returned HTTP {exc.code}.")
        if body:
            log(f"Ollama response: {body}")
        log("Debug hints:")
        log(f"- Confirm this exact model exists: ollama list | findstr {OLLAMA_MODEL}")
        log(f"- Pull it if needed: ollama pull {OLLAMA_MODEL}")
        raise RuntimeError(f"Ollama HTTP error: {exc.code}") from exc
    except urllib.error.URLError as exc:
        log("Could not reach Ollama.")
        log("Debug hints:")
        log("- Confirm Ollama is running.")
        log(f"- Confirm the API is reachable at {OLLAMA_URL}.")
        log(f"- Confirm the model is pulled: ollama pull {OLLAMA_MODEL}")
        raise RuntimeError(f"Ollama request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama returned invalid JSON: {exc}") from exc

    clean_text = str(result.get("response", "")).strip()
    return clean_text


def warm_ollama_model() -> None:
    """Warm the Ollama model in the background to reduce first-use latency."""
    if not ENABLE_OLLAMA_REFINEMENT:
        return

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": "Reply with OK.",
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {
            "temperature": 0.0,
            "num_predict": 2,
        },
    }
    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            response.read()
        log(f"Ollama model warmed ({time.perf_counter() - started:.2f}s).")
    except Exception as exc:
        log(f"Ollama warm-up skipped: {exc}")


def paste_text(text: str) -> None:
    """Copy polished text to the clipboard and paste into the active app."""
    if not text:
        log("Nothing to paste.")
        return

    old_clipboard: Optional[str] = None
    try:
        old_clipboard = pyperclip.paste()
    except Exception:
        old_clipboard = None

    try:
        pyperclip.copy(text)
        time.sleep(PASTE_DELAY_SECONDS)
        pyautogui.hotkey("ctrl", "v")
        log("Pasted polished text into the active window.")
    except Exception as exc:
        log("Could not paste into the active window.")
        log("Debug hints:")
        log("- Make sure a text field is focused.")
        log("- Try running the terminal normally, not as a different privilege level.")
        log(f"Raw error: {exc}")
    finally:
        if not KEEP_ON_CLIPBOARD and old_clipboard is not None:
            try:
                time.sleep(PASTE_DELAY_SECONDS)
                pyperclip.copy(old_clipboard)
            except Exception:
                pass


def process_recording() -> None:
    """Save, transcribe, polish, and paste the captured recording."""
    global is_processing, last_metrics_text, last_final_text, live_partial_text, _prev_dictation_had_trailing_space

    wav_path: Optional[Path] = None
    audio_seconds = 0.0
    status_after_processing = "idle"
    started_at = time.perf_counter()
    try:
        stage_started = time.perf_counter()
        wav_path, audio_seconds = write_temp_wav()
        log(f"Saved temporary WAV: {wav_path.name} ({time.perf_counter() - stage_started:.2f}s)")

        stage_started = time.perf_counter()
        raw_text = transcribe_audio(wav_path)
        transcribe_seconds = time.perf_counter() - stage_started
        if not raw_text:
            log("Whisper returned no text. Try speaking closer to the mic.")
            return

        raw_text = apply_dictionary(raw_text)
        log(f"Raw transcript ({transcribe_seconds:.2f}s): {raw_text}")

        stage_started = time.perf_counter()
        clean_text = refine_with_ollama(raw_text)
        ollama_seconds = time.perf_counter() - stage_started

        if not clean_text:
            log("Ollama returned an empty response; pasting raw transcript instead.")
            clean_text = raw_text

        log(f"Final text ({ollama_seconds:.2f}s): {clean_text}")

        if LOWERCASE_FIRST and clean_text:
            clean_text = clean_text[0].lower() + clean_text[1:]
        if STRIP_TRAILING_PERIOD and clean_text.endswith("."):
            clean_text = clean_text[:-1]
        if SPACE_BETWEEN_DICTATIONS:
            if clean_text and not _prev_dictation_had_trailing_space:
                clean_text = " " + clean_text
            _prev_dictation_had_trailing_space = clean_text[-1:].isspace()

        last_final_text = clean_text
        paste_text(clean_text)
        total_seconds = time.perf_counter() - started_at
        log(f"Total processing time: {total_seconds:.2f}s")
        if SHOW_METRICS:
            last_metrics_text = (
                f"total {total_seconds:.1f}s  ·  whisper {transcribe_seconds:.1f}  ·  llm {ollama_seconds:.1f}"
            )
        record_history(raw_text, clean_text, transcribe_seconds, ollama_seconds, audio_seconds, wav_path)

    except Exception as exc:
        status_after_processing = "error"
        set_status("error")
        log(f"Processing failed: {exc}")
    finally:
        if wav_path and wav_path.exists():
            try:
                wav_path.unlink()
            except OSError:
                log(f"Could not delete temporary WAV: {wav_path}")
        with recording_lock:
            is_processing = False
        live_partial_text = ""
        if not shutdown_event.is_set():
            set_status(status_after_processing)


def toggle_recording() -> None:
    """Hotkey target."""
    if is_recording:
        stop_recording()
    else:
        start_recording()


class HoldHotkey:
    """Hold-to-talk combo tracking: fires on_start once every key in the combo is
    pressed, on_stop the moment any one of them is released. Mirrors keyboard.HotKey's
    parse() format but needs its own press/release state since HotKey itself only
    supports fire-on-complete (toggle) semantics."""

    def __init__(self, combo_str: str, on_start, on_stop) -> None:
        self._keys = set(keyboard.HotKey.parse(combo_str))
        self._pressed: set = set()
        self._active = False
        self._on_start = on_start
        self._on_stop = on_stop

    def press(self, key) -> None:
        if key not in self._keys:
            return
        self._pressed.add(key)
        if not self._active and self._keys <= self._pressed:
            self._active = True
            self._on_start()

    def release(self, key) -> None:
        if key not in self._keys:
            return
        self._pressed.discard(key)
        if self._active:
            self._active = False
            self._on_stop()


# -----------------------------
# Interactive hotkey capture (pynput)
# -----------------------------
# The settings UI records a new hotkey by starting a dedicated, short-lived
# pynput Listener here rather than reading browser keydown events: pynput sees
# keys exactly like the real global listener does (correct physical layout,
# AltGr, Win key), where the browser maps e.code through a US layout and
# WebView2 reserves some chords (e.g. Ctrl+Shift+J) as devtools accelerators.

hotkey_capture_active = False  # guard: on_press/on_release below skip dispatch while a capture is running
_hotkey_capture_lock = threading.Lock()
_hotkey_capture_state = "idle"  # idle | recording | done | cancelled
_hotkey_capture_result: Optional[str] = None
_hotkey_capture_live: list = []  # tokens held right now, for live UI feedback
_hotkey_capture_listener: Optional[keyboard.Listener] = None
_hotkey_capture_timer: Optional[threading.Timer] = None
_hotkey_capture_done = True  # True whenever no capture is in flight (blocks stray finishes)
_hotkey_capture_down: dict = {}  # raw pynput key -> token, keys currently held
_hotkey_capture_seen: dict = {}  # token -> True, every modifier token seen this capture, in order

_CAPTURE_MODIFIER_TOKENS = {"<ctrl>", "<alt>", "<alt_gr>", "<shift>", "<cmd>"}
_CAPTURE_MODIFIER_ORDER = ["<ctrl>", "<alt>", "<alt_gr>", "<shift>", "<cmd>"]

_CAPTURE_KEY_TOKENS = {
    keyboard.Key.ctrl_l: "<ctrl>", keyboard.Key.ctrl_r: "<ctrl>", keyboard.Key.ctrl: "<ctrl>",
    keyboard.Key.shift_l: "<shift>", keyboard.Key.shift_r: "<shift>", keyboard.Key.shift: "<shift>",
    keyboard.Key.alt_l: "<alt>", keyboard.Key.alt: "<alt>",
    # Windows reports AltGr as alt_gr, and sometimes as alt_r alongside a
    # synthetic ctrl_l (see the quirk handling in _capture_on_press) — both
    # mean the same physical AltGr key, so both map to <alt_gr>.
    keyboard.Key.alt_gr: "<alt_gr>", keyboard.Key.alt_r: "<alt_gr>",
    keyboard.Key.cmd: "<cmd>", keyboard.Key.cmd_l: "<cmd>", keyboard.Key.cmd_r: "<cmd>",
    keyboard.Key.space: "<space>", keyboard.Key.tab: "<tab>", keyboard.Key.enter: "<enter>",
    keyboard.Key.backspace: "<backspace>", keyboard.Key.delete: "<delete>",
    keyboard.Key.up: "<up>", keyboard.Key.down: "<down>", keyboard.Key.left: "<left>", keyboard.Key.right: "<right>",
    keyboard.Key.home: "<home>", keyboard.Key.end: "<end>",
    keyboard.Key.page_up: "<page_up>", keyboard.Key.page_down: "<page_down>",
}


def _capture_token(key) -> Optional[str]:
    """Map one pynput key event to the token string keyboard.HotKey.parse() accepts."""
    if key in _CAPTURE_KEY_TOKENS:
        return _CAPTURE_KEY_TOKENS[key]
    if isinstance(key, keyboard.Key):
        return f"<{key.name}>"  # other Key enum members (Fn keys, etc.), e.g. Key.f5 -> "<f5>"
    vk = getattr(key, "vk", None)
    char = getattr(key, "char", None)
    if char and char.isprintable():
        return char.lower()
    # Ctrl+letter/digit produces a control character in .char (e.g. "\x0a" for
    # Ctrl+J) on Windows; pynput still exposes the real key via .vk.
    if vk is not None:
        if 65 <= vk <= 90:
            return chr(vk).lower()
        if 48 <= vk <= 57:
            return chr(vk)
    return None


def _finalize_combo(modifier_tokens, final_token: Optional[str] = None) -> Optional[str]:
    """Build a HotKey.parse()-compatible combo string from modifier tokens (any
    order, duplicates allowed) plus an optional trailing non-modifier key token."""
    mods = [t for t in _CAPTURE_MODIFIER_ORDER if t in modifier_tokens]
    if final_token:
        mods.append(final_token)
    return "+".join(mods) if mods else None


def _capture_valid(combo: Optional[str]) -> bool:
    if not combo:
        return False
    try:
        keyboard.HotKey.parse(combo)
        return True
    except Exception:
        return False


def _format_hotkey_display(combo: Optional[str]) -> Optional[str]:
    """Mirrors ui/app.js's formatHotkeyDisplay() so both sides agree on the label."""
    if not combo:
        return None
    display = combo
    for token, name in (("<ctrl>", "Ctrl"), ("<shift>", "Shift"), ("<alt_gr>", "AltGr"), ("<alt>", "Alt"), ("<cmd>", "Win")):
        display = display.replace(token, name)
    display = display.replace("<", "").replace(">", "")
    return " + ".join(display.split("+"))


def _capture_finish(combo: Optional[str], state: str) -> None:
    global _hotkey_capture_done, hotkey_capture_active
    if _hotkey_capture_done:
        return
    _hotkey_capture_done = True
    hotkey_capture_active = False
    if _hotkey_capture_timer is not None:
        _hotkey_capture_timer.cancel()
    if _hotkey_capture_listener is not None:
        try:
            _hotkey_capture_listener.stop()
        except Exception:
            pass
    with _hotkey_capture_lock:
        globals()["_hotkey_capture_state"] = state
        globals()["_hotkey_capture_result"] = combo
        globals()["_hotkey_capture_live"] = []


def _capture_on_press(key) -> None:
    if key == keyboard.Key.esc:
        _capture_finish(None, "cancelled")
        return
    if key in (keyboard.Key.alt_gr, keyboard.Key.alt_r):
        # Windows AltGr quirk: strip the synthetic ctrl_l that rides along with it.
        _hotkey_capture_down.pop(keyboard.Key.ctrl_l, None)
        _hotkey_capture_seen.pop("<ctrl>", None)
    token = _capture_token(key)
    if token is None:
        return
    _hotkey_capture_down[key] = token
    _hotkey_capture_seen[token] = True
    with _hotkey_capture_lock:
        globals()["_hotkey_capture_live"] = list(_hotkey_capture_seen.keys())
    if token in _CAPTURE_MODIFIER_TOKENS:
        return  # pending modifier only; wait for a real key or full release
    mods = [t for t in _hotkey_capture_down.values() if t in _CAPTURE_MODIFIER_TOKENS]
    combo = _finalize_combo(mods, token)
    if _capture_valid(combo):
        _capture_finish(combo, "done")


def _capture_on_release(key) -> None:
    _hotkey_capture_down.pop(key, None)
    if _hotkey_capture_down or _hotkey_capture_done:
        return
    # Every key is now up and nothing finalized yet, so only modifier(s) were
    # pressed (e.g. AltGr alone, or Win alone) — accept them as the combo.
    combo = _finalize_combo(list(_hotkey_capture_seen.keys()))
    if _capture_valid(combo):
        _capture_finish(combo, "done")
    else:
        _capture_finish(None, "cancelled")


def on_press(key) -> None:
    try:
        canonical = listener.canonical(key)
    except Exception:
        return

    if hotkey_capture_active:
        return

    try:
        if ACTIVATION_MODE == "hold" and hold_hotkey is not None:
            hold_hotkey.press(canonical)
        elif hotkey is not None:
            hotkey.press(canonical)
    except Exception:
        pass

    if paste_last_hotkey is not None:
        try:
            paste_last_hotkey.press(canonical)
        except Exception:
            pass


def on_release(key) -> None:
    try:
        canonical = listener.canonical(key)
    except Exception:
        return

    if hotkey_capture_active:
        return

    try:
        if ACTIVATION_MODE == "hold" and hold_hotkey is not None:
            hold_hotkey.release(canonical)
        elif hotkey is not None:
            hotkey.release(canonical)
    except Exception:
        pass

    if paste_last_hotkey is not None:
        try:
            paste_last_hotkey.release(canonical)
        except Exception:
            pass


def print_startup_banner() -> None:
    log("Local Flow is starting.")
    log(f"Hotkey: {HOTKEY}")
    log(f"Ollama model: {OLLAMA_MODEL}")
    log(f"Ollama URL: {OLLAMA_URL}")
    if STT_ENGINE == "parakeet":
        log(f"STT engine: parakeet ({PARAKEET_MODEL_NAME}, CUDA)")
    else:
        log(f"STT engine: whisper ({WHISPER_MODEL_NAME} on {WHISPER_DEVICE}, {WHISPER_COMPUTE_TYPE})")
    log(f"Transcription language: {TRANSCRIPTION_LANGUAGE}")
    log(f"Ollama refinement: {'on' if ENABLE_OLLAMA_REFINEMENT else 'off'}")
    log(f"Tray icon: {'on' if ENABLE_TRAY_ICON else 'off'}")
    log(f"Floating overlay: {'on' if ENABLE_FLOATING_OVERLAY else 'off'}")
    log("Press Ctrl+C in this terminal to quit.")


if __name__ == "__main__":
    _settings_existed = SETTINGS_PATH.exists()
    load_settings()
    log("Settings loaded from settings.json" if _settings_existed else "Settings file created with defaults")

    hide_console_window()
    print_startup_banner()
    start_floating_overlay()
    start_tray_icon()

    try:
        if STT_ENGINE == "parakeet":
            model = load_parakeet_model()
            started = time.perf_counter()
            model.recognize(np.zeros(SAMPLE_RATE, dtype=np.float32), sample_rate=SAMPLE_RATE)
            log(f"Parakeet warm-up ({time.perf_counter() - started:.2f}s).")
        else:
            load_whisper_model()
    except Exception as exc:
        log(f"Startup failed: {exc}")
        sys.exit(1)

    threading.Thread(target=warm_ollama_model, daemon=True).start()

    if ACTIVATION_MODE == "hold":
        hold_hotkey = HoldHotkey(HOTKEY, start_recording, stop_recording)
    else:
        hotkey = keyboard.HotKey(keyboard.HotKey.parse(HOTKEY), toggle_recording)

    if PASTE_LAST_HOTKEY:
        try:
            paste_last_hotkey = keyboard.HotKey(
                keyboard.HotKey.parse(PASTE_LAST_HOTKEY), lambda: paste_text(last_final_text)
            )
        except Exception as exc:
            log(f"Invalid paste-last hotkey {PASTE_LAST_HOTKEY!r}: {exc}")
            paste_last_hotkey = None

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)

    # pywebview must create its window and run its GUI loop on the main thread on Windows.
    # It owns this thread until its window is destroyed (the quit path), so the pynput
    # listener (which runs its own thread) is started from on_webview_ready instead of here.
    webview_ran = False
    try:
        import webview

        html_path = APP_DIR / "ui" / "index.html"
        webview_window = webview.create_window(
            "Local Flow",
            url=html_path.as_uri(),
            js_api=UiApi(),
            width=1280,
            height=900,
            min_size=(1000, 700),
            background_color="#0B0E13",
            hidden=True,
        )

        def on_closing() -> bool:
            webview_window.hide()
            return False

        webview_window.events.closing += on_closing

        def on_webview_ready() -> None:
            listener.start()
            if os.environ.get("LOCAL_FLOW_OPEN_UI") == "1":  # debug: open the main window on start
                open_main_window()

        webview.start(func=on_webview_ready)
        webview_ran = True
    except Exception as exc:
        log(f"Main window unavailable, dictation continues without it: {exc}")
        webview_window = None

    if webview_ran:
        # webview.start() returned because its window was destroyed (the quit path).
        if not shutdown_event.is_set():
            quit_app()
    else:
        try:
            listener.start()
            listener.join()
        except KeyboardInterrupt:
            log("Exiting Local Flow.")
        finally:
            shutdown_event.set()

    if input_stream is not None:
        try:
            input_stream.stop()
            input_stream.close()
        except Exception:
            pass
    if tray_icon is not None:
        try:
            tray_icon.stop()
        except Exception:
            pass
