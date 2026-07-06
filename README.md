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
