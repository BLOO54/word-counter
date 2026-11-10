"""Download the speech model into models/base.en so it gets packed into the app.

Run once before building:  python tools/get_model.py
"""

from pathlib import Path

from huggingface_hub import snapshot_download

target = Path(__file__).resolve().parent.parent / "models" / "base.en"
target.mkdir(parents=True, exist_ok=True)
snapshot_download("Systran/faster-whisper-base.en", local_dir=str(target),
                  allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*"])
files = sorted(p.name for p in target.iterdir() if p.is_file())
print("model files:", files)
assert (target / "model.bin").exists(), "model.bin missing"
