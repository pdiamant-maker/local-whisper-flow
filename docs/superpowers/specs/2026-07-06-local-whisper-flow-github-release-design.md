# Local Whisper Flow — GitHub Release Design

**Date:** 2026-07-06
**Status:** Approved (pending spec review)
**Author:** Petros + Claude

## Goal

Publish the existing single-file dictation app (`local_flow.py`) as a public GitHub
repository named **`local-whisper-flow`** that anyone can clone and run on a plain
laptop **CPU-first**, with NVIDIA CUDA/GPU documented as an optional speed unlock.

This is not a rewrite. The app already works. Scope is: light code cleanup, a small
tray-icon feature, a live-metrics toggle, clear CPU-first documentation, licensing,
privacy-safe git hygiene, and the actual publish (private first, then public).

## Naming

- Repo slug: `local-whisper-flow`
- README / branding title: **Local Whisper Flow** (the "Local" prefix separates it
  from the commercial *Wispr Flow* and signals the offline/private angle).
- Internal app strings (tray tooltip, window titles, logs) keep saying "Local Flow" —
  no need to touch them.

## Decisions (from brainstorming)

1. **Hardware framing:** CPU-first. Ship CPU defaults; GPU is a documented upgrade.
2. **License:** MIT.
3. **Metrics:** both a README benchmark table AND a live in-app timing readout,
   gated behind a `SHOW_METRICS` toggle.
4. **Configuration:** minimal-but-clean — keep settings as constants at the top of
   `local_flow.py`, cleaned and grouped under one "EDIT THESE" block. No config
   file / `.env` (explicitly deferred).
5. **Added feature this release:** animate the tray icon to match overlay states.
6. **Publish flow:** create the GitHub repo **private first**, review committed
   contents, then flip to public.

## Repo structure

```
local-whisper-flow/
├── local_flow.py          # the app (lightly cleaned, CPU-first defaults)
├── requirements.txt       # pinned deps (core + tray)
├── README.md              # main doc, CPU-first
├── LICENSE                # MIT
├── .gitignore             # logs, *.wav, __pycache__, *.bak, internal notes
└── docs/                  # optional images later (e.g. overlay GIF)
```

Files that stay LOCAL and are NOT committed (covered by `.gitignore`):
`local_flow.log`, `local_flow_launcher.log`, `local_flow_*.wav`, all `*.bak-*`
backups, `local_flow_launcher.pyw`, `openwhispr-ollama-setup-prompt.md`,
`summary.md`. Useful content from the notes is folded into the README instead.

## Code changes to `local_flow.py`

All changes are surgical; the pipeline logic is untouched.

### 1. Config cleanup (approach A)
- `EXTRA_CUDA_DLL_DIRS` → empty list by default, commented as an **advanced,
  optional, GPU-only** setting. Remove the personal `C:\Users\petro\...` and
  DaVinci Resolve paths.
- CPU-first defaults: ship `WHISPER_DEVICE = "cpu"` and
  `WHISPER_COMPUTE_TYPE = "int8"`, with a clearly commented block showing how to
  switch to `"cuda"` / `"float16"` for NVIDIA GPUs.
- Group all user-facing knobs under one `# ==== EDIT THESE ====` header with a
  one-line comment per knob.
- Keep `OLLAMA_MODEL = "qwen2.5:7b-instruct"` as default; document 3b as the
  lower-latency / lower-spec alternative.
- Document (in comments + README) the `TRANSCRIPTION_LANGUAGE = "en"` tradeoff:
  `"en"` skips language detection for speed; auto-detect enables other languages
  at a latency cost.

### 2. Live metrics toggle
- New constant `SHOW_METRICS = True`.
- When on, after each dictation the overlay briefly shows a compact timing line —
  e.g. `total 0.7s · whisper 0.3 · llm 0.1` — reusing the existing overlay canvas
  (no new window). The timing values already exist in `process_recording()`;
  route them to the overlay via the existing overlay queue mechanism (extend the
  queued message or add a parallel channel for the metrics string).
- When off, overlay behaves exactly as today.

### 3. Animated tray icon
- Currently `make_tray_image()` draws a static glyph and the tray icon only updates
  on status change. Make the tray icon animate to reflect state (at minimum: a
  clear visual difference / motion for recording vs idle vs processing).
- Implementation note: pystray updates the icon by reassigning `tray_icon.icon`.
  A lightweight background loop (or reuse of an existing timer) regenerates the
  image every N ms while in an animated state. Keep it cheap; this is cosmetic.
  Must not block or interfere with the hotkey/audio threads. Gate behind the
  existing `ENABLE_TRAY_ICON`.

## Documentation (`README.md`)

CPU-first ordering so a no-GPU laptop user succeeds on first try:

1. **What it is** + one-line demo / overlay GIF (GIF optional, can come later).
2. **Features** — global hotkey, fully local & private, animated overlay + tray,
   GPU-optional.
3. **Quick start (CPU)** — install Python deps (`requirements.txt`) → install & run
   Ollama → `ollama pull qwen2.5:7b-instruct` → run `python local_flow.py` → press
   `Ctrl+Shift+K`.
4. **Ollama setup** — install link, pulling the model (note 3b for lower-spec
   machines), confirming the server is running at `localhost:11434`.
5. **Optional: GPU acceleration (NVIDIA / CUDA)** — the upgrade path: switch the
   two device constants, install cuDNN/cuBLAS runtime, the `EXTRA_CUDA_DLL_DIRS`
   advanced setting, expected speedup.
6. **Performance & metrics** — the benchmark table + the `SHOW_METRICS` toggle.
7. **Configuration reference** — every knob in the EDIT THESE block, one line each,
   including the language tradeoff.
8. **Troubleshooting** — mic permissions, missing CUDA DLLs, Ollama model missing.
   Source material already exists as the app's log hint messages.
9. **Roadmap** — deferred items (see below).
10. **License** — MIT.

### Benchmark table (filled from real numbers before publish)

| Stage | GPU (CUDA) | CPU |
|---|---|---|
| Transcribe (Whisper large-v3-turbo) | ~0.3s | ~3–4s (use a smaller model) |
| Refine (Ollama qwen2.5:7b) | ~0.1s | slower |
| **End-to-end** | **~0.7s** | **several seconds** |

CPU note in README: recommend `base.en` / `small.en` Whisper models for usable CPU
latency, and `qwen2.5:3b-instruct` for refinement.

## requirements.txt

Pinned versions of the known deps:
`faster-whisper`, `sounddevice`, `soundfile`, `pynput`, `pyperclip`, `pyautogui`,
`numpy`, plus `pystray` and `pillow` (tray). Pin to the versions currently working
on Petros's machine (capture via `pip freeze` for these packages during
implementation).

## Privacy & git hygiene ⚠️

- `local_flow.log` contains **real dictated transcripts** — a genuine privacy leak
  if committed. It is `.gitignore`d. Before the first commit, explicitly verify
  `git status` shows no log, no `.wav`, no `.bak`, and no personal-path content.
- The `.gitignore` is created and committed BEFORE any app files are staged.

## Publishing steps

1. `git init` (done early to allow committing this spec).
2. Create `.gitignore` first (done), then commit spec.
3. Clean the code, write README/LICENSE/requirements.txt.
4. Stage app files; manually verify nothing sensitive is included.
5. Create the GitHub repo **private**:
   - If `gh` CLI is installed & authenticated: `gh repo create local-whisper-flow --private --source=. --remote=origin`.
   - If not: provide click-by-click GitHub.com instructions (new private repo,
     add remote, push) since Petros is new to this.
6. Push. Petros reviews the private repo.
7. Flip to public from GitHub settings when satisfied.

## Roadmap (deferred — documented, not built)

- Config file / `.env` for settings.
- Live settings-UI editing (model / device / language in the Tk settings window).
- Multi-model speed presets (fast: `small.en` / balanced: `large-v3-turbo` /
  quality: `large-v3`).
- PyInstaller `.exe` packaged build (bundling CUDA DLLs + model is fiddly — own
  milestone).
- GitHub Actions CI (blocked on there being a test suite first).
- Command mode for rewriting selected text; custom vocabulary/dictionary.

## Out of scope / non-goals

- No rewrite of the transcription/refinement pipeline.
- No change to the hotkey, audio capture, or paste behavior.
- No new runtime dependencies beyond the existing ones.
- No CI, no packaging, no config file in this release.

## Success criteria

- A person with no NVIDIA GPU can clone the repo, follow the README, and dictate
  into a text field successfully using only CPU.
- No personal paths or private logs exist anywhere in the committed repo.
- Tray icon and overlay both visibly reflect recording / idle / processing states.
- `SHOW_METRICS = True` shows a live timing line; `False` hides it.
- Repo is created private, reviewed, then made public by Petros.
