"""
Local Flow: a lightweight Windows dictation helper.

Hotkey:
    Ctrl + Shift + K toggles recording on/off.

Install dependencies:
    pip install faster-whisper sounddevice soundfile pynput pyperclip pyautogui numpy

Optional tray icon:
    pip install pystray pillow

Notes:
    - Ollama must already be running at OLLAMA_URL.
    - The selected Ollama model must already be pulled locally.
    - faster-whisper with device="cuda" requires a working NVIDIA CUDA runtime.
"""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
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
HOTKEY = "<ctrl>+<shift>+k"

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:7b-instruct"  # Higher polish. Use "qwen2.5:3b-instruct" for lower latency.
OLLAMA_TIMEOUT_SECONDS = 90
OLLAMA_KEEP_ALIVE = "30m"
ENABLE_OLLAMA_REFINEMENT = True
OLLAMA_NUM_PREDICT = 96

# Whisper model. Smaller = faster, less accurate. Good options:
#   "base.en" / "small.en"  -> best for CPU (low latency)
#   "large-v3-turbo"        -> best accuracy, needs a GPU to stay fast
WHISPER_MODEL_NAME = "base.en"

# Device: "cpu" works everywhere. Switch to "cuda" if you have an NVIDIA GPU
# with the CUDA runtime installed (see the GPU section of the README).
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"  # GPU users: set WHISPER_DEVICE="cuda" and this to "float16".

# ADVANCED / GPU ONLY: extra folders to search for CUDA runtime DLLs (cuBLAS/cuDNN).
# Leave empty on CPU. On Windows GPU setups, add the folder(s) containing
# cublas64_12.dll / cudnn*.dll if they are not already on your PATH, e.g.:
#   EXTRA_CUDA_DLL_DIRS = [r"C:\path\to\cuda\bin"]
EXTRA_CUDA_DLL_DIRS: list[str] = []

SAMPLE_RATE = 16000
CHANNELS = 1
PASTE_DELAY_SECONDS = 0.08
ENABLE_TRAY_ICON = True
ENABLE_FLOATING_OVERLAY = True
FLOATING_OVERLAY_SHOW_WHEN_IDLE = False
STARTUP_READY_OVERLAY_SECONDS = 4
TRANSCRIPTION_LANGUAGE = "en"  # Skips language detection for faster English dictation.
HIDE_CONSOLE_ON_START = os.environ.get("LOCAL_FLOW_DEBUG_CONSOLE") != "1"
SHOW_METRICS = True  # Show a live "total 0.7s · whisper 0.3 · llm 0.1" line on the overlay after each dictation.

APP_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = Path(__file__).resolve()
LOG_FILE = APP_DIR / "local_flow.log"
SHORTCUT_NAME = f"{APP_NAME}.lnk"


SYSTEM_PROMPT = """You clean raw speech-to-text dictation.

Rules:
- Remove filler words, false starts, repeated stutters, and verbal hesitation.
- Preserve the speaker's meaning.
- Add correct punctuation, capitalization, and paragraph breaks when useful.
- Keep technical terms, names, numbers, and code-like text intact.
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
listener: Optional[keyboard.Listener] = None
hotkey: Optional[keyboard.HotKey] = None
tray_icon = None
shutdown_event = threading.Event()
current_status = "starting"
show_idle_overlay_until = 0.0
last_metrics_text = ""  # Populated after each dictation when SHOW_METRICS is on.
_dll_directory_handles = []

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
    arguments = f'"{SCRIPT_PATH}"'
    description = f"{APP_NAME} local dictation"
    icon_location = f"{target_path},0"
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


def open_settings_window(icon=None, item=None) -> None:
    """Open a small settings window for shortcuts and startup behavior."""

    def run_window() -> None:
        try:
            import tkinter as tk
            from tkinter import messagebox
        except ImportError:
            log("Settings window unavailable because Tkinter is not installed.")
            return

        root = tk.Tk()
        root.title(f"{APP_NAME} Settings")
        root.resizable(False, False)
        root.attributes("-topmost", True)

        status_var = tk.StringVar()

        def refresh_status() -> None:
            desktop_status = "installed" if get_desktop_shortcut_path().exists() else "not installed"
            startup_status = "enabled" if get_startup_shortcut_path().exists() else "disabled"
            status_var.set(
                f"Desktop launcher: {desktop_status}\n"
                f"Start with Windows: {startup_status}\n"
                f"Script: {SCRIPT_PATH}\n"
                f"Log: {LOG_FILE}"
            )

        def run_action(label: str, action) -> None:
            try:
                result = action()
                refresh_status()
                if result:
                    messagebox.showinfo(APP_NAME, f"{label} complete:\n{result}")
                else:
                    messagebox.showinfo(APP_NAME, f"{label} complete.")
            except Exception as exc:
                log(f"{label} failed: {exc}")
                messagebox.showerror(APP_NAME, f"{label} failed:\n{exc}")

        frame = tk.Frame(root, padx=18, pady=16)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text=APP_NAME, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(
            frame,
            text="Local dictation helper",
            font=("Segoe UI", 9),
            fg="#555555",
        ).pack(anchor="w", pady=(0, 12))

        tk.Label(
            frame,
            textvariable=status_var,
            justify="left",
            anchor="w",
            width=72,
            bg="#F3F5F7",
            padx=10,
            pady=8,
        ).pack(fill="x", pady=(0, 12))

        tk.Button(
            frame,
            text="Create Desktop Launcher",
            command=lambda: run_action("Create desktop launcher", create_desktop_shortcut),
            width=30,
        ).pack(anchor="w", pady=3)

        tk.Button(
            frame,
            text="Start Local Flow When Windows Starts",
            command=lambda: run_action("Enable startup", enable_start_with_windows),
            width=30,
        ).pack(anchor="w", pady=3)

        tk.Button(
            frame,
            text="Stop Starting With Windows",
            command=lambda: run_action("Disable startup", disable_start_with_windows),
            width=30,
        ).pack(anchor="w", pady=3)

        tk.Button(
            frame,
            text="Open App Folder",
            command=lambda: open_path(APP_DIR),
            width=30,
        ).pack(anchor="w", pady=(12, 3))

        tk.Button(
            frame,
            text="Open Log File",
            command=lambda: open_path(LOG_FILE),
            width=30,
        ).pack(anchor="w", pady=3)

        tk.Button(frame, text="Close", command=root.destroy, width=30).pack(anchor="w", pady=(12, 0))

        refresh_status()
        root.mainloop()

    threading.Thread(target=run_window, daemon=True).start()


def configure_cuda_dll_search() -> None:
    """Expose known CUDA runtime folders to this Python process."""
    if WHISPER_DEVICE.lower() != "cuda":
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


def make_tray_image(status: str):
    """Create a tiny colored tray icon for the current status."""
    if not TRAY_IMPORTS_AVAILABLE:
        return None

    color = TRAY_COLORS.get(status, TRAY_COLORS["idle"])
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, 58, 58), fill=color, outline=(255, 255, 255), width=3)
    draw.rectangle((29, 18, 35, 38), fill=(255, 255, 255))
    draw.arc((22, 28, 42, 48), 0, 180, fill=(255, 255, 255), width=4)
    draw.line((32, 46, 32, 54), fill=(255, 255, 255), width=4)
    return image


def set_status(status: str) -> None:
    """Update terminal/tray status."""
    global current_status, show_idle_overlay_until

    current_status = status
    if status == "idle":
        show_idle_overlay_until = time.time() + STARTUP_READY_OVERLAY_SECONDS
    overlay_queue.put(status)
    if tray_icon is not None and TRAY_IMPORTS_AVAILABLE:
        tray_icon.icon = make_tray_image(status)
        tray_icon.title = f"Local Flow - {status}"


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

        width = 118
        height = 76
        canvas = tk.Canvas(root, width=width, height=height, bg="#101820", highlightthickness=0)
        canvas.pack()

        def place_window() -> None:
            screen_width = root.winfo_screenwidth()
            screen_height = root.winfo_screenheight()
            x = max(0, screen_width - width - 28)
            y = max(0, screen_height - height - 96)
            root.geometry(f"{width}x{height}+{x}+{y}")

        # Equalizer geometry and per-status motion energy (eased for smooth transitions).
        bar_xs = (24, 33, 42, 51, 60)
        bar_mid = 36
        bar_max_half = 19
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
                canvas.create_line(x, bar_mid - half, x, bar_mid + half, fill=color, width=6, capstyle=tk.ROUND)
            canvas.create_text(88, 28, text=label, fill="#FFFFFF", font=("Segoe UI", 11, "bold"))
            canvas.create_text(88, 48, text="Flow", fill="#AAB6C4", font=("Segoe UI", 9))
            if SHOW_METRICS and last_metrics_text and status in ("idle", "processing"):
                canvas.create_text(
                    width // 2, height - 7,
                    text=last_metrics_text, fill="#7A8896", font=("Segoe UI", 7),
                )

        def poll() -> None:
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


def start_tray_icon() -> None:
    """Start an optional Windows tray icon."""
    global tray_icon

    if not ENABLE_TRAY_ICON:
        return

    if not TRAY_IMPORTS_AVAILABLE:
        log("Tray icon disabled. Install optional dependencies: pip install pystray pillow")
        return

    menu = pystray.Menu(
        pystray.MenuItem("Toggle recording", lambda icon, item: toggle_recording()),
        pystray.MenuItem("Settings...", open_settings_window),
        pystray.MenuItem("Quit Local Flow", quit_app),
    )
    tray_icon = pystray.Icon("Local Flow", make_tray_image(current_status), "Local Flow", menu)
    thread = threading.Thread(target=tray_icon.run, daemon=True)
    thread.start()


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
    global input_stream, is_recording

    with recording_lock:
        if is_recording:
            return

        if is_processing:
            log("Still processing the previous dictation. Please wait a moment.")
            return

        recorded_chunks.clear()
        drain_audio_queue()

        try:
            input_stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=audio_callback,
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
        log("RECORDING ON  | Speak now. Press Ctrl+Shift+K again to stop.")


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


def write_temp_wav() -> Path:
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
    return wav_path


def transcribe_audio(wav_path: Path) -> str:
    """Transcribe the WAV file with faster-whisper."""
    model = load_whisper_model()
    try:
        segments, info = model.transcribe(
            str(wav_path),
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

    log(
        "Transcription complete "
        f"(language={info.language}, probability={info.language_probability:.2f})."
    )
    return text


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


def process_recording() -> None:
    """Save, transcribe, polish, and paste the captured recording."""
    global is_processing, last_metrics_text

    wav_path: Optional[Path] = None
    status_after_processing = "idle"
    started_at = time.perf_counter()
    try:
        stage_started = time.perf_counter()
        wav_path = write_temp_wav()
        log(f"Saved temporary WAV: {wav_path.name} ({time.perf_counter() - stage_started:.2f}s)")

        stage_started = time.perf_counter()
        raw_text = transcribe_audio(wav_path)
        transcribe_seconds = time.perf_counter() - stage_started
        if not raw_text:
            log("Whisper returned no text. Try speaking closer to the mic.")
            return

        log(f"Raw transcript ({transcribe_seconds:.2f}s): {raw_text}")

        stage_started = time.perf_counter()
        clean_text = refine_with_ollama(raw_text)
        ollama_seconds = time.perf_counter() - stage_started

        if not clean_text:
            log("Ollama returned an empty response; pasting raw transcript instead.")
            clean_text = raw_text

        log(f"Final text ({ollama_seconds:.2f}s): {clean_text}")
        paste_text(clean_text)
        total_seconds = time.perf_counter() - started_at
        log(f"Total processing time: {total_seconds:.2f}s")
        if SHOW_METRICS:
            last_metrics_text = (
                f"total {total_seconds:.1f}s  ·  whisper {transcribe_seconds:.1f}  ·  llm {ollama_seconds:.1f}"
            )

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
        if not shutdown_event.is_set():
            set_status(status_after_processing)


def toggle_recording() -> None:
    """Hotkey target."""
    if is_recording:
        stop_recording()
    else:
        start_recording()


def on_press(key) -> None:
    try:
        hotkey.press(listener.canonical(key))
    except Exception:
        pass


def on_release(key) -> None:
    try:
        hotkey.release(listener.canonical(key))
    except Exception:
        pass


def print_startup_banner() -> None:
    log("Local Flow is starting.")
    log(f"Hotkey: {HOTKEY}")
    log(f"Ollama model: {OLLAMA_MODEL}")
    log(f"Ollama URL: {OLLAMA_URL}")
    log(f"Whisper: {WHISPER_MODEL_NAME} on {WHISPER_DEVICE} ({WHISPER_COMPUTE_TYPE})")
    log(f"Transcription language: {TRANSCRIPTION_LANGUAGE}")
    log(f"Ollama refinement: {'on' if ENABLE_OLLAMA_REFINEMENT else 'off'}")
    log(f"Tray icon: {'on' if ENABLE_TRAY_ICON else 'off'}")
    log(f"Floating overlay: {'on' if ENABLE_FLOATING_OVERLAY else 'off'}")
    log("Press Ctrl+C in this terminal to quit.")


if __name__ == "__main__":
    hide_console_window()
    print_startup_banner()
    start_floating_overlay()
    start_tray_icon()

    try:
        load_whisper_model()
    except Exception as exc:
        log(f"Startup failed: {exc}")
        sys.exit(1)

    threading.Thread(target=warm_ollama_model, daemon=True).start()

    hotkey = keyboard.HotKey(keyboard.HotKey.parse(HOTKEY), toggle_recording)
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)

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
