"""The speech models you can pick under "Accuracy".

Plain English: "Fast" comes inside the app. "Better" and "Best" are bigger
versions of the same speech recognition (Whisper) that catch words much more
reliably; they're downloaded from Hugging Face the first time you pick them
and kept in %LOCALAPPDATA%\\WordCounter\\models.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import urllib.request
from pathlib import Path

from . import APP_VERSION
from .config import resource_dir

log = logging.getLogger(__name__)

# key: (Hugging Face repo or None if built in, label, size text, what it's like)
MODELS = {
    "fast": (None, "Fast", "built in", "quickest, misses more words"),
    "better": ("Systran/faster-whisper-small.en", "Better", "about 480 MB", "catches a lot more, still quick"),
    "best": ("Systran/faster-distil-whisper-large-v3", "Best", "about 1.5 GB",
             "great with slang and names, a bit slower"),
    "max": ("Systran/faster-whisper-large-v3", "Max", "about 3 GB",
            "the most accurate - needs the graphics card to keep up"),
}
ORDER = ("fast", "better", "best", "max")
NEEDED_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "vocabulary.json",
                "preprocessor_config.json")
HF = "https://huggingface.co"
USER_AGENT = f"WordCounter/{APP_VERSION} (https://github.com/BLOO54/word-counter)"


def models_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "WordCounter" / "models"


class ModelStore:
    def __init__(self, folder: Path | None = None, opener=None):
        self.folder = folder or models_dir()
        self.opener = opener or urllib.request.build_opener()

    def path(self, name: str) -> Path:
        if MODELS[name][0] is None:
            return resource_dir() / "models" / "base.en"
        return self.folder / name

    def installed(self, name: str) -> bool:
        p = self.path(name)
        if MODELS[name][0] is None:
            return (p / "model.bin").exists()
        try:
            info = json.loads((p / "ready.json").read_text(encoding="utf-8"))
            files = [f["path"] for f in info.get("files", [])]
        except (OSError, ValueError):
            return False
        return bool(files) and all((p / f).exists() for f in files)

    def remove(self, name: str) -> None:
        if MODELS[name][0] is not None:
            shutil.rmtree(self.path(name), ignore_errors=True)

    def _get(self, url: str, timeout=30):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return self.opener.open(req, timeout=timeout)

    def file_list(self, name: str) -> list[dict]:
        repo = MODELS[name][0]
        with self._get(f"{HF}/api/models/{repo}/tree/main") as r:
            items = json.loads(r.read().decode("utf-8"))
        files = []
        for it in items:
            if it.get("type") == "file" and it.get("path") in NEEDED_FILES:
                lfs = it.get("lfs") or {}
                files.append({"path": it["path"], "size": int(lfs.get("size") or it.get("size") or 0),
                              "sha256": lfs.get("oid") if lfs else None})
        if not any(f["path"] == "model.bin" for f in files):
            raise RuntimeError("the model files weren't found online")
        return files

    def download(self, name: str, progress=None, cancel: threading.Event | None = None) -> Path:
        """Download a model. progress(done_bytes, total_bytes). Raises on failure."""
        files = self.file_list(name)
        total = sum(f["size"] for f in files)
        target = self.path(name)
        target.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target).free < total + 200 * 1024 * 1024:
            raise RuntimeError(f"not enough free disk space (needs {total / 1e9:.1f} GB)")
        (target / "ready.json").unlink(missing_ok=True)
        repo = MODELS[name][0]
        done = 0
        for f in files:
            dest = target / f["path"]
            if dest.exists() and dest.stat().st_size == f["size"] and f["size"] > 0:
                done += f["size"]
                if progress:
                    progress(done, total)
                continue
            part = dest.with_suffix(dest.suffix + ".part")
            h = hashlib.sha256()
            with self._get(f"{HF}/{repo}/resolve/main/{f['path']}", timeout=60) as r, open(part, "wb") as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        out.close()
                        part.unlink(missing_ok=True)
                        raise InterruptedError("download cancelled")
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            if f["sha256"] and h.hexdigest() != f["sha256"]:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"{f['path']} didn't download correctly - please try again")
            os.replace(part, dest)
        (target / "ready.json").write_text(json.dumps({"repo": repo, "files": files}), encoding="utf-8")
        log.info("model %s downloaded (%.0f MB)", name, total / 1e6)
        return target


def folder_size(path: Path) -> int:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0
