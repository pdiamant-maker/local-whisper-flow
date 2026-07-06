# Local Whisper Flow — GitHub Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the existing `local_flow.py` dictation app as the public GitHub repo `local-whisper-flow` — CPU-first defaults, personal paths removed, a `SHOW_METRICS` live timing readout, an animated tray icon, and full docs — created private first, then flipped public.

**Architecture:** Surgical edits to the single existing file `local_flow.py` (config block, overlay `render()`, tray icon) plus three new top-level files (`README.md`, `LICENSE`, `requirements.txt`). The transcription/refinement/paste pipeline is untouched. Publish via `gh` CLI if available, else manual GitHub.com steps.

**Tech Stack:** Python 3, Tkinter (overlay), pystray + Pillow (tray), faster-whisper, Ollama, git + GitHub.

**Verification note:** This project has no test framework and the spec defers adding one (no logic worth a unit harness — it's config, cosmetics, and docs). Checks below are real runnable commands: `python -m py_compile`, launching the app and reading `local_flow.log`, and `grep` assertions. Do NOT invent a pytest suite.

**IMPORTANT — privacy gate:** `local_flow.log` contains real dictated transcripts. It is gitignored. Before ANY commit that stages app files (Task 7+), run `git status` and confirm no `.log`, `.wav`, `.bak`, or personal-path file is staged.

---

### Task 1: Config cleanup — CPU-first defaults, remove personal paths, add SHOW_METRICS

**Files:**
- Modify: `local_flow.py` (config constants block, ~lines 59–85)

- [ ] **Step 1: Read the current config block**

Run: `sed -n '55,90p' local_flow.py` (or Read the file). Confirm the current values match what this task edits: `OLLAMA_MODEL = "qwen2.5:7b-instruct"`, `WHISPER_DEVICE = "cuda"`, `WHISPER_COMPUTE_TYPE = "float16"`, and `EXTRA_CUDA_DLL_DIRS` containing `C:\Users\petro\...` and the DaVinci Resolve path.

- [ ] **Step 2: Replace the whisper device + CUDA DLL block with CPU-first defaults**

Replace this current block:

```python
WHISPER_MODEL_NAME = "large-v3-turbo"  # Use "small.en" or "base.en" if you want even lower latency.
WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE_TYPE = "float16"  # CPU fallback: set WHISPER_DEVICE = "cpu" and this to "int8".
EXTRA_CUDA_DLL_DIRS = [
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve",
    r"C:\Users\petro\AppData\Local\Programs\Ollama\lib\ollama\cuda_v12",
]
```

with:

```python
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
```

- [ ] **Step 3: Add the SHOW_METRICS toggle near the other UI flags**

Find this line (~line 80):

```python
ENABLE_TRAY_ICON = True
```

Immediately after the block of `ENABLE_*` / overlay flags (after `HIDE_CONSOLE_ON_START = ...`, ~line 85), add:

```python
SHOW_METRICS = True  # Show a live "total 0.7s · whisper 0.3 · llm 0.1" line on the overlay after each dictation.
```

- [ ] **Step 4: Add an EDIT-THESE header above the config section**

Find the comment banner (~line 55):

```python
# -----------------------------
# User configuration
# -----------------------------
```

Replace with:

```python
# =============================================================================
# EDIT THESE — user configuration
# Every knob you would normally change lives in this section. Defaults run on
# CPU with no GPU required. See README.md for what each setting does.
# =============================================================================
```

- [ ] **Step 5: Verify it still compiles**

Run: `python -m py_compile local_flow.py`
Expected: no output, exit 0.

- [ ] **Step 6: Verify personal paths are gone**

Run: `grep -n "petro\|DaVinci\|Blackmagic" local_flow.py`
Expected: no matches (empty output).

- [ ] **Step 7: Commit**

```bash
git add local_flow.py
git commit -m "Config: CPU-first defaults, remove personal paths, add SHOW_METRICS toggle"
```

---

### Task 2: Live metrics readout on the overlay

**Files:**
- Modify: `local_flow.py` — runtime state globals (~line 120), overlay `render()` (~lines 499–538), `process_recording()` (~lines 847–893)

- [ ] **Step 1: Add a module-level global for the last metrics string**

Find (~line 121):

```python
current_status = "starting"
show_idle_overlay_until = 0.0
```

Add directly after:

```python
last_metrics_text = ""  # Populated after each dictation when SHOW_METRICS is on.
```

- [ ] **Step 2: Populate it in process_recording after total time is known**

In `process_recording()`, find:

```python
        log(f"Final text ({ollama_seconds:.2f}s): {clean_text}")
        paste_text(clean_text)
        log(f"Total processing time: {time.perf_counter() - started_at:.2f}s")
```

Replace with:

```python
        log(f"Final text ({ollama_seconds:.2f}s): {clean_text}")
        paste_text(clean_text)
        total_seconds = time.perf_counter() - started_at
        log(f"Total processing time: {total_seconds:.2f}s")
        if SHOW_METRICS:
            global last_metrics_text
            last_metrics_text = (
                f"total {total_seconds:.1f}s  ·  whisper {transcribe_seconds:.1f}  ·  llm {ollama_seconds:.1f}"
            )
```

(`·` is the "·" middot; `global last_metrics_text` is safe alongside the existing `global is_processing` — Python allows multiple `global` statements in one function, but to be clean, add `last_metrics_text` to the existing `global is_processing` line at the top of the function instead: change `global is_processing` to `global is_processing, last_metrics_text` and drop the inner `global` line.)

Final form — top of `process_recording()` change:

```python
def process_recording() -> None:
    """Save, transcribe, polish, and paste the captured recording."""
    global is_processing, last_metrics_text
```

and the inner block becomes:

```python
        total_seconds = time.perf_counter() - started_at
        log(f"Total processing time: {total_seconds:.2f}s")
        if SHOW_METRICS:
            last_metrics_text = (
                f"total {total_seconds:.1f}s  ·  whisper {transcribe_seconds:.1f}  ·  llm {ollama_seconds:.1f}"
            )
```

- [ ] **Step 3: Draw the metrics line in the overlay render()**

In `render()` inside `start_floating_overlay()`, find the two text lines at the end:

```python
            canvas.create_text(88, 28, text=label, fill="#FFFFFF", font=("Segoe UI", 11, "bold"))
            canvas.create_text(88, 48, text="Flow", fill="#AAB6C4", font=("Segoe UI", 9))
```

Add directly after them:

```python
            if SHOW_METRICS and last_metrics_text and status in ("idle", "processing"):
                canvas.create_text(
                    width // 2, height - 7,
                    text=last_metrics_text, fill="#7A8896", font=("Segoe UI", 7),
                )
```

(`width` and `height` are already in scope inside `run_overlay`. `last_metrics_text` is read as a module global — no `global` needed for a read.)

- [ ] **Step 4: Verify it compiles**

Run: `python -m py_compile local_flow.py`
Expected: no output, exit 0.

- [ ] **Step 5: Runtime check — launch, dictate once, confirm the metrics line is logged**

Run (background): `LOCAL_FLOW_DEBUG_CONSOLE=1 python local_flow.py`
Then press `Ctrl+Shift+K`, say one sentence, press again. Read `local_flow.log`.
Expected: a `Total processing time: N.NNs` line appears, and (visually) the overlay shows the `total ... · whisper ... · llm ...` line for ~4s after pasting. Stop the app.

- [ ] **Step 6: Commit**

```bash
git add local_flow.py
git commit -m "Add SHOW_METRICS live timing readout on the overlay"
```

---

### Task 3: Animated tray icon

**Files:**
- Modify: `local_flow.py` — `make_tray_image()` (~lines 420–432), `start_tray_icon()` (~lines 542–560). `import math` is already present (added earlier).

- [ ] **Step 1: Replace make_tray_image with an animated, phase-driven equalizer**

Replace the current function:

```python
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
```

with:

```python
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
```

- [ ] **Step 2: Add a background animation loop and start it in start_tray_icon**

In `start_tray_icon()`, find the end of the function:

```python
    tray_icon = pystray.Icon("Local Flow", make_tray_image(current_status), "Local Flow", menu)
    thread = threading.Thread(target=tray_icon.run, daemon=True)
    thread.start()
```

Replace with:

```python
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
```

- [ ] **Step 3: Verify it compiles**

Run: `python -m py_compile local_flow.py`
Expected: no output, exit 0.

- [ ] **Step 4: Confirm set_status still calls make_tray_image compatibly**

Run: `grep -n "make_tray_image" local_flow.py`
Expected: three call sites — inside `set_status` (`make_tray_image(status)`), inside `start_tray_icon` init (`make_tray_image(current_status)`), and inside `animate_tray` (`make_tray_image(current_status, phase)`). All are valid because `phase` defaults to `0.0`. No change needed to `set_status`.

- [ ] **Step 5: Runtime check — launch and confirm no tray errors**

Run (background): `LOCAL_FLOW_DEBUG_CONSOLE=1 python local_flow.py`
Read `local_flow.log`. Expected: startup banner, "Whisper model loaded", no tray traceback. Visually confirm the tray icon bars move and change color on `Ctrl+Shift+K`. Stop the app.

- [ ] **Step 6: Commit**

```bash
git add local_flow.py
git commit -m "Animate the tray icon to match recording/idle/processing states"
```

---

### Task 4: requirements.txt

**Files:**
- Create: `requirements.txt`

- [ ] **Step 1: Capture the working versions on this machine**

Run: `pip freeze | grep -iE "faster-whisper|sounddevice|soundfile|pynput|pyperclip|pyautogui|numpy|pystray|pillow"`
Note the exact `==` versions printed.

- [ ] **Step 2: Write requirements.txt using those versions**

Create `requirements.txt` with the captured versions (example shape — substitute the real numbers from Step 1):

```
faster-whisper==<version>
sounddevice==<version>
soundfile==<version>
pynput==<version>
pyperclip==<version>
pyautogui==<version>
numpy==<version>
# Optional: system tray icon
pystray==<version>
pillow==<version>
```

- [ ] **Step 3: Verify it installs clean into a throwaway check (dry run)**

Run: `pip install --dry-run -r requirements.txt`
Expected: resolves without conflict (already-satisfied is fine).

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "Add pinned requirements.txt"
```

---

### Task 5: LICENSE (MIT)

**Files:**
- Create: `LICENSE`

- [ ] **Step 1: Write the MIT license**

Create `LICENSE` with the standard MIT text, copyright line `Copyright (c) 2026 Petros Diamantopoulos` (confirm preferred name/handle with the user; default to this):

```
MIT License

Copyright (c) 2026 Petros Diamantopoulos

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 2: Commit**

```bash
git add LICENSE
git commit -m "Add MIT license"
```

---

### Task 6: README.md

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write the README (CPU-first ordering)**

Create `README.md` with these sections, in this order. Use the benchmark numbers filled in Task 8; until then leave the table with the rough values shown and mark the GPU row as "measured on author's machine".

````markdown
# Local Whisper Flow

A lightweight, fully local dictation tool for Windows. Press a hotkey, speak,
and your cleaned-up text is pasted into whatever app you're in. No cloud, no
account, no data leaving your machine.

Speech is transcribed locally with [faster-whisper]; the raw transcript is then
tidied (filler words removed, punctuation added) by a local [Ollama] model.

## Features

- **Global hotkey** — `Ctrl + Shift + K` to start/stop, anywhere.
- **Fully local & private** — audio and text never leave your computer.
- **Animated indicator** — a floating on-screen bar meter and tray icon show
  recording (red) / idle (green) / processing (amber).
- **CPU by default, GPU optional** — runs on any laptop; NVIDIA CUDA makes it fast.
- **Live metrics** — optional timing readout so you can see exactly how fast it is.

## Quick start (CPU — works on any machine)

1. **Install Python 3.10+** and the dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
2. **Install [Ollama]** and start it (it runs a local server at
   `http://localhost:11434`).
3. **Pull the cleanup model:**
   ```powershell
   ollama pull qwen2.5:7b-instruct
   ```
   On a lower-spec machine use the smaller `qwen2.5:3b-instruct` instead and set
   `OLLAMA_MODEL` accordingly in `local_flow.py`.
4. **Run it:**
   ```powershell
   python local_flow.py
   ```
5. **Dictate:** focus any text field, press `Ctrl + Shift + K`, speak, press again.

The defaults use the CPU-friendly `base.en` Whisper model. First run downloads
the model automatically.

## Ollama setup

- Download Ollama from https://ollama.com and install it.
- Confirm it's running: `ollama list` should respond without error.
- Pull the model used for cleanup (see Quick start). The model is what turns
  raw speech-to-text into clean punctuated sentences.
- The app talks to Ollama at `http://localhost:11434/api/generate` (configurable
  via `OLLAMA_URL`).

## Optional: GPU acceleration (NVIDIA / CUDA)

On an NVIDIA GPU with the CUDA runtime, transcription is dramatically faster and
you can use the higher-accuracy `large-v3-turbo` model in real time.

1. In `local_flow.py`, set:
   ```python
   WHISPER_MODEL_NAME = "large-v3-turbo"
   WHISPER_DEVICE = "cuda"
   WHISPER_COMPUTE_TYPE = "float16"
   ```
2. Install the NVIDIA CUDA runtime libraries — you need **cuBLAS for CUDA 12**
   and **cuDNN 9 for CUDA 12** on your PATH.
3. If Python can't find `cublas64_12.dll` / `cudnn*.dll`, add the folder(s)
   containing them to `EXTRA_CUDA_DLL_DIRS` in `local_flow.py`.

## Performance & metrics

Set `SHOW_METRICS = True` (default) to see a live timing line on the overlay
after each dictation, e.g. `total 0.7s · whisper 0.3 · llm 0.1`.

| Stage | GPU (CUDA) | CPU |
|---|---|---|
| Transcribe (Whisper) | ~0.3s (large-v3-turbo) | ~3–4s (use `base.en`/`small.en`) |
| Refine (Ollama) | ~0.1s (7b) | slower (use `3b`) |
| **End-to-end** | **~0.7s** | **a few seconds** |

*GPU numbers measured on the author's machine. Your times will vary.*

For usable CPU latency, prefer the `base.en` or `small.en` Whisper model and the
`qwen2.5:3b-instruct` cleanup model.

## Configuration reference

All settings are constants at the top of `local_flow.py` under the
`EDIT THESE` header:

| Setting | What it does |
|---|---|
| `HOTKEY` | The global start/stop shortcut. |
| `OLLAMA_MODEL` | Ollama model used to clean the transcript. |
| `ENABLE_OLLAMA_REFINEMENT` | Turn text cleanup on/off (off = raw transcript). |
| `WHISPER_MODEL_NAME` | Whisper model — smaller is faster, bigger is more accurate. |
| `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE` | `cpu`/`int8` or `cuda`/`float16`. |
| `EXTRA_CUDA_DLL_DIRS` | Advanced/GPU: extra folders to find CUDA DLLs. |
| `TRANSCRIPTION_LANGUAGE` | `"en"` skips language detection for speed. Set to auto-detect (see faster-whisper docs) for other languages, at a latency cost. |
| `SHOW_METRICS` | Show the live timing line on the overlay. |
| `ENABLE_TRAY_ICON` / `ENABLE_FLOATING_OVERLAY` | Toggle the indicators. |

## Troubleshooting

- **Ollama model error** — run `ollama list` to confirm the model is pulled;
  `ollama pull <model>` if missing.
- **CUDA DLL not found** (`cublas64_12.dll` / `cudnn`) — the CUDA runtime isn't
  on your PATH; install cuBLAS/cuDNN for CUDA 12 or add their folder to
  `EXTRA_CUDA_DLL_DIRS`. Or just use CPU mode.
- **Nothing pastes** — make sure a text field is focused; check mic permissions.
- **Logs** — the app writes `local_flow.log` next to the script.

## Roadmap

Planned, not yet built:
- Settings file / in-app settings editing (model, device, language).
- Speed presets (fast / balanced / quality).
- Packaged `.exe` build (no Python install required).
- Command mode for rewriting selected text; custom vocabulary.

## License

MIT — see [LICENSE](LICENSE).

[faster-whisper]: https://github.com/SYSTRAN/faster-whisper
[Ollama]: https://ollama.com
````

- [ ] **Step 2: Sanity-check the markdown renders (no broken fences)**

Run: `grep -c '```' README.md`
Expected: an even number (all code fences closed).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Add README with CPU-first setup, Ollama guide, metrics, and roadmap"
```

---

### Task 7: Gitignore working dirs, stage app, privacy verification

**Files:**
- Modify: `.gitignore` (add `.impeccable/`)

- [ ] **Step 1: Add the Impeccable working dir to .gitignore**

Append to `.gitignore`:

```
.impeccable/
```

- [ ] **Step 2: Stage everything and inspect what's tracked**

```bash
git add -A
git status
```

- [ ] **Step 3: PRIVACY GATE — confirm nothing sensitive is staged**

Run: `git status --short | grep -iE "\.log$|\.wav$|\.bak|petro|summary.md|launcher"`
Expected: **empty output.** If ANY line appears, stop and fix `.gitignore` before continuing.

Also run: `git ls-files` and confirm the tracked set is exactly: `.gitignore`, `LICENSE`, `README.md`, `local_flow.py`, `requirements.txt`, and the two `docs/superpowers/...` files. Nothing else.

- [ ] **Step 4: Final content scan of the tracked app file**

Run: `git grep -nI "petro\|DaVinci\|Blackmagic\|C:\\\\Users" -- local_flow.py`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git commit -m "Ignore Impeccable working dir; finalize tracked file set for release"
```

---

### Task 8: Fill real benchmark numbers, then publish (private first)

**Files:**
- Modify: `README.md` (benchmark table numbers)

- [ ] **Step 1: Measure real numbers on this machine**

Temporarily set GPU mode (`WHISPER_DEVICE="cuda"`, `WHISPER_COMPUTE_TYPE="float16"`, `WHISPER_MODEL_NAME="large-v3-turbo"`), run the app, dictate 2–3 sentences, and read the `whisper`, `llm`, and `total` timings from `local_flow.log`. Then (optionally) repeat in CPU mode with `base.en` for a rough CPU figure. Revert the committed defaults back to CPU-first afterward (do not commit GPU defaults).

- [ ] **Step 2: Update the README benchmark table with the measured values**

Edit the table in `README.md` to use the real GPU numbers (and CPU if measured). Keep the "measured on the author's machine" caveat.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Fill benchmark table with measured timings"
```

- [ ] **Step 4: Check whether the gh CLI is available and authenticated**

Run: `gh auth status`
- If it reports a logged-in account → use Step 5a.
- If `gh` is missing or not logged in → use Step 5b.

- [ ] **Step 5a: Create the PRIVATE repo with gh and push**

```bash
gh repo create local-whisper-flow --private --source=. --remote=origin --push
```
Expected: repo created private, `origin` set, initial branch pushed. Print the repo URL.

- [ ] **Step 5b: Manual GitHub.com path (if no gh)**

Give the user these exact steps:
1. Go to https://github.com/new — Repository name: `local-whisper-flow`, visibility: **Private**, do NOT initialize with README/license/gitignore (we already have them). Create.
2. Copy the commands GitHub shows under "push an existing repository", which are:
   ```bash
   git branch -M main
   git remote add origin https://github.com/<your-username>/local-whisper-flow.git
   git push -u origin main
   ```
3. Run them in `C:\Users\petro\Desktop\Whisperflow`.

- [ ] **Step 6: Verify the pushed repo contents match the tracked set**

Run: `gh repo view local-whisper-flow --web` (or open the URL) and confirm ONLY the intended files are present — no `.log`, `.wav`, `.bak`, `summary.md`, or personal paths. This is the review checkpoint before going public.

- [ ] **Step 7: Hand off the public flip to the user**

The repo is private and pushed. The user reviews it on GitHub, then flips it to public via **Settings → General → Danger Zone → Change visibility → Public** when satisfied. (Do not flip it automatically — the user explicitly wants to review first.)

---

## Self-Review

**Spec coverage:**
- CPU-first defaults → Task 1 ✓
- Remove personal paths → Task 1 (+ verified Task 7) ✓
- EDIT THESE grouped config → Task 1 ✓
- SHOW_METRICS live readout → Task 1 (flag) + Task 2 (behavior) ✓
- Animated tray icon → Task 3 ✓
- requirements.txt → Task 4 ✓
- MIT LICENSE → Task 5 ✓
- README CPU-first + Ollama + GPU + metrics table + language tradeoff + roadmap + troubleshooting → Task 6 ✓
- Benchmark table with real numbers → Task 8 ✓
- Privacy / gitignore / no log committed → `.gitignore` (done pre-plan) + Task 7 gate ✓
- Private-first publish, then user flips public → Task 8 ✓
- Roadmap documented not built → README Roadmap (Task 6) ✓

**Placeholder scan:** requirements.txt versions are captured live in Task 4 Step 1 (not a placeholder — it's a measured value). Benchmark numbers filled in Task 8 from real measurement. No "TBD"/"handle edge cases"/vague steps remain.

**Type/name consistency:** `SHOW_METRICS`, `last_metrics_text`, `make_tray_image(status, phase=0.0)`, `_TRAY_ENERGY`, `EXTRA_CUDA_DLL_DIRS`, `total_seconds` used consistently across Tasks 1–3. `make_tray_image` new signature is back-compatible with the existing `set_status` call (verified Task 3 Step 4).
