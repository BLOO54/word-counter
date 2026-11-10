"""Saving your words, counts and settings between runs (in %APPDATA%\\WordCounter)."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SOURCES = ("mic", "discord_mic", "discord", "pc", "both")
SOURCE_PARTS = {"mic": ["mic"], "discord_mic": ["discord", "mic"], "discord": ["discord"],
                "pc": ["pc"], "both": ["pc", "mic"]}


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "WordCounter"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resource_dir() -> Path:
    """Where bundled files (the speech model, icons) live - inside the .exe build or next to the code."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    words: list[dict] = field(default_factory=list)   # [{"text": "bro", "count": 3}]
    source: str = "mic"
    beep: bool = True
    always_on_top: bool = False
    show_text: bool = True
    similar: bool = True
    accuracy: str = "fast"
    use_gpu: bool = False
    milestone: int = 0
    careful: bool = False
    vram_measured: dict = field(default_factory=dict)   # model -> bytes on the graphics card
    live: bool = False
    geometry: str = ""

    @classmethod
    def load(cls) -> "Settings":
        path = app_data_dir() / "settings.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except Exception:
            log.exception("settings file unreadable - starting fresh")
            return cls()
        s = cls()
        for k in asdict(s):
            if k in raw:
                setattr(s, k, raw[k])
        s.words = [{"text": str(w.get("text", "")), "count": int(w.get("count", 0))}
                   for w in s.words if isinstance(w, dict) and str(w.get("text", "")).strip()]
        if s.source not in SOURCES:
            s.source = "mic"
        s.vram_measured = {str(k): int(v) for k, v in (s.vram_measured or {}).items()
                           if isinstance(v, (int, float)) and v > 0} if isinstance(s.vram_measured, dict) else {}
        if s.milestone not in (0, 10, 25, 50, 100):
            s.milestone = 0
        if s.accuracy not in ("fast", "better", "best", "max"):
            s.accuracy = "fast"
        return s

    def save(self) -> None:
        path = app_data_dir() / "settings.json"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            log.exception("couldn't save settings")
