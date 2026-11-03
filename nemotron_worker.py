"""Nemotron STT worker. Runs in .venv-nemo (NeMo + PyTorch) so the app's own venv stays untouched.

Setup (once, from the app folder; Python 3.12, NVIDIA GPU):
  py -3.12 -m venv .venv-nemo
  .venv-nemo\\Scripts\\python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
  .venv-nemo\\Scripts\\python -m pip install "nemo_toolkit[asr]==3.0.0" soundfile

Protocol on stdin/stdout, one request at a time:
  request:  a JSON line {"lang": "auto", "n": <samples>} followed by n float32 samples (16 kHz mono)
  response: a JSON line {"text": "..."} or {"error": "..."}
The first line written is {"ready": true} once the model is loaded. EOF on stdin ends the worker.
"""
import json
import os
import sys

# NeMo prints to stdout; keep the real stdout for replies and send everything else to stderr.
_reply = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)
sys.stdout = sys.stderr

import numpy as np  # noqa: E402
import nemo.collections.asr as nemo_asr  # noqa: E402

MODEL_NAME = sys.argv[1] if len(sys.argv) > 1 else "nvidia/nemotron-labs-unified-asr-0.6b"


def reply(obj: dict) -> None:
    _reply.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _reply.flush()


def main() -> None:
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=MODEL_NAME).cuda().eval()
    model.transcribe([np.zeros(16000, dtype=np.float32)], target_lang="en", verbose=False)  # warm-up
    reply({"ready": True})

    stdin = sys.stdin.buffer
    while True:
        header = stdin.readline()
        if not header:
            return  # app closed the pipe
        try:
            request = json.loads(header)
            audio = np.frombuffer(stdin.read(request["n"] * 4), dtype=np.float32)
            # ponytail: arrays, not paths — NeMo's temp manifest can't be deleted on Windows (WinError 32)
            output = model.transcribe([audio], target_lang=request.get("lang", "auto"), verbose=False)
            reply({"text": output[0].text if output else ""})
        except Exception as exc:
            reply({"error": str(exc)})


if __name__ == "__main__":
    main()
