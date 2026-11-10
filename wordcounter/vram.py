"""How much graphics memory (VRAM) things use.

Plain English: NVIDIA's driver comes with a small library (nvml.dll) that can tell
how much of the graphics card's memory is in use. We read it before and after
loading a model to measure what that model really takes on YOUR card.
Until a model has been measured, a typical number is shown instead.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# typical graphics memory per model on the card (float16, including working memory), in GB
ESTIMATE_GB = {"fast": 0.4, "better": 0.8, "best": 1.8, "max": 3.5}
CAREFUL_EXTRA_GB = 0.2

_nvml = None
_handle = None


class _Memory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


def _load():
    global _nvml, _handle
    if _nvml is not None:
        return _handle is not None
    _nvml = False
    if sys.platform != "win32":
        return False
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    for path in (Path(windir) / "System32" / "nvml.dll",
                 Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvml.dll"):
        if path.exists():
            try:
                lib = ctypes.WinDLL(str(path))
                if lib.nvmlInit_v2() != 0:
                    continue
                h = ctypes.c_void_p()
                if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
                    continue
                _nvml, _handle = lib, h
                return True
            except OSError:
                continue
    return False


def card_memory() -> tuple[int, int] | None:
    """(used bytes, total bytes) on the first NVIDIA card, or None if it can't be read."""
    try:
        if not _load():
            return None
        m = _Memory()
        if _nvml.nvmlDeviceGetMemoryInfo(_handle, ctypes.byref(m)) != 0:
            return None
        return int(m.used), int(m.total)
    except Exception:
        log.debug("couldn't read graphics memory", exc_info=True)
        return None


def gb(n_bytes: float) -> str:
    return f"{n_bytes / 1e9:.1f} GB"


def model_vram_text(key: str, measured: dict, careful: bool = False) -> str:
    """'1.8 GB (measured)' or '~1.8 GB'."""
    if key in measured and measured[key] > 0:
        return f"{gb(measured[key])} (measured)"
    est = ESTIMATE_GB.get(key, 0) + (CAREFUL_EXTRA_GB if careful else 0)
    return f"~{est:.1f} GB"
