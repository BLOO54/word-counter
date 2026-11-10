"""Run the speech recognition on an NVIDIA graphics card.

Plain English: Whisper is a lot faster on a graphics card, but it needs two
of NVIDIA's maths libraries (cuBLAS and cuDNN). They're too big to put in the
installer for everyone, so they're downloaded once - straight from NVIDIA's
official Python packages on PyPI - into %LOCALAPPDATA%\\WordCounter\\cuda, only
when you switch "Use my graphics card" on. If anything goes wrong, the app
keeps working on the processor (CPU).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path

from . import APP_VERSION

log = logging.getLogger(__name__)

# PyPI package -> major version we need (CUDA 12 builds, cuDNN 9 - what faster-whisper uses)
PACKAGES = [
    ("nvidia-cublas-cu12", "12."),
    ("nvidia-cudnn-cu12", "9."),
    ("nvidia-cuda-runtime-cu12", "12."),
    ("nvidia-cuda-nvrtc-cu12", "12."),
]
USER_AGENT = f"WordCounter/{APP_VERSION} (https://github.com/BLOO54/word-counter)"
_activated = False


def libs_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "WordCounter" / "cuda"


def has_nvidia_driver() -> bool:
    """True if an NVIDIA graphics driver is installed (nvcuda.dll comes with it)."""
    if sys.platform != "win32":
        return False
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    return (Path(windir) / "System32" / "nvcuda.dll").exists()


def installed() -> bool:
    d = libs_dir()
    try:
        info = json.loads((d / "ready.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(info.get("dlls")) and all((d / n).exists() for n in info["dlls"])


def activate() -> bool:
    """Let Windows find the downloaded libraries. Call before the speech engine loads."""
    global _activated
    if _activated:
        return True
    if not installed():
        return False
    d = str(libs_dir())
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    try:
        os.add_dll_directory(d)          # Windows only
    except (AttributeError, OSError):
        pass
    _activated = True
    log.info("graphics card libraries made available from %s", d)
    return True


def _get(url: str, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def _version_key(v: str):
    out = []
    for part in v.split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return out


def plan() -> list[dict]:
    """The Windows wheel to download for each package (newest matching version)."""
    wheels = []
    for name, major in PACKAGES:
        with _get(f"https://pypi.org/pypi/{name}/json", timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        best = None
        for ver in sorted(data.get("releases", {}), key=_version_key, reverse=True):
            if not ver.startswith(major) or any(t in ver for t in ("a", "b", "rc", "dev")):
                continue
            for f in data["releases"][ver]:
                fn = f.get("filename", "")
                if fn.endswith(".whl") and "win_amd64" in fn and not f.get("yanked"):
                    best = {"package": name, "version": ver, "url": f["url"], "size": int(f.get("size") or 0),
                            "sha256": (f.get("digests") or {}).get("sha256")}
                    break
            if best:
                break
        if not best:
            raise RuntimeError(f"couldn't find {name} for Windows")
        wheels.append(best)
    return wheels


def download(progress=None) -> Path:
    """Download + unpack the libraries. progress(done_bytes, total_bytes)."""
    wheels = plan()
    total = sum(w["size"] for w in wheels)
    target = libs_dir()
    target.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(target).free < total * 3:
        raise RuntimeError(f"not enough free disk space (needs about {total * 2.5 / 1e9:.1f} GB)")
    (target / "ready.json").unlink(missing_ok=True)
    done, dlls = 0, []
    for w in wheels:
        part = target / (w["package"] + ".whl.part")
        h = hashlib.sha256()
        with _get(w["url"]) as r, open(part, "wb") as out:
            while True:
                chunk = r.read(1024 * 512)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if w["sha256"] and h.hexdigest() != w["sha256"]:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{w['package']} didn't download correctly - please try again")
        with zipfile.ZipFile(part) as z:
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                if name.lower().endswith(".dll") and "/bin/" in name:
                    base = name.rsplit("/", 1)[-1]
                    with z.open(info) as src, open(target / base, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    dlls.append(base)
        part.unlink(missing_ok=True)
        log.info("graphics card library %s %s unpacked", w["package"], w["version"])
    if not any(d.lower().startswith("cublas64") for d in dlls) or not any(d.lower().startswith("cudnn") for d in dlls):
        raise RuntimeError("the downloaded libraries were missing files")
    (target / "ready.json").write_text(json.dumps({"wheels": wheels, "dlls": sorted(set(dlls))}, indent=1),
                                       encoding="utf-8")
    return target


def size_text() -> str:
    try:
        return f"about {sum(w['size'] for w in plan()) / 1e9:.1f} GB"
    except Exception:
        return "about 1 GB"


def remove() -> None:
    shutil.rmtree(libs_dir(), ignore_errors=True)


def describe_error(e: Exception) -> str:
    text = str(e) or type(e).__name__
    low = text.lower()
    if "cuda driver" in low or "insufficient" in low:
        return "your NVIDIA driver is too old - update it with the NVIDIA app / GeForce Experience"
    if "no cuda" in low or "cuda device" in low or "no device" in low:
        return "no NVIDIA graphics card was found"
    return text


