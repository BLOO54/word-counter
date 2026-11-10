"""Listening: the microphone and/or everything the PC plays, cut into short spoken pieces.

Plain English: sound comes in as tiny 0.1-second slices. We keep slices while
someone is talking, and when they pause (or after ~6 seconds) we hand that
piece of speech to the speech recognition.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import warnings
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

RATE = 16000                 # what the speech recognition wants
FRAME = RATE // 10           # 0.1 s
MAX_FRAMES = 60              # cut long talking into ~6 s pieces
END_SILENCE = 5              # 0.5 s of quiet ends a piece
PREROLL = 5                  # keep 0.5 s before speech starts so first words aren't clipped


class Segmenter:
    """Turns a stream of 0.1 s frames into pieces of speech."""

    def __init__(self, emit: Callable[[np.ndarray], None], adaptive: bool = True):
        self.emit = emit
        self.adaptive = adaptive        # mic: learn the room's background noise. PC sound: fixed level.
        self.floor = 0.003
        self.frames: list[np.ndarray] = []
        self.levels: list[float] = []
        self.speech = 0
        self.quiet = 0

    def threshold(self) -> float:
        return max(0.006, self.floor * 2.5) if self.adaptive else 0.004

    def add(self, frame: np.ndarray) -> None:
        level = float(np.sqrt(np.mean(frame * frame))) if frame.size else 0.0
        if self.adaptive:
            if level < self.floor:
                self.floor = 0.5 * self.floor + 0.5 * level
            else:
                self.floor = min(self.floor * 1.002, 0.05)
            self.floor = max(self.floor, 0.0005)
        loud = level > self.threshold()

        if not self.speech and not loud:
            self.frames.append(frame)
            self.levels.append(level)
            if len(self.frames) > PREROLL:
                self.frames.pop(0)
                self.levels.pop(0)
            return

        self.frames.append(frame)
        self.levels.append(level)
        if loud:
            self.speech += 1
            self.quiet = 0
        else:
            self.quiet += 1

        if self.quiet >= END_SILENCE:
            self._flush(len(self.frames))
        elif len(self.frames) >= MAX_FRAMES:
            # cut at the quietest moment of the last 1.5 s, so we don't split a word in half
            tail = self.levels[-15:]
            cut = len(self.frames) - 15 + int(np.argmin(tail)) + 1
            self._flush(cut, keep_going=True)

    def _flush(self, upto: int, keep_going: bool = False) -> None:
        piece, rest = self.frames[:upto], self.frames[upto:]
        rest_levels = self.levels[upto:]
        if self.speech >= 3 and piece:
            self.emit(np.concatenate(piece))
        self.frames, self.levels = rest, rest_levels
        self.quiet = 0
        self.speech = sum(1 for lv in rest_levels if lv > self.threshold()) if keep_going else 0
        if not keep_going:
            self.frames, self.levels = [], []


def _com_init() -> None:
    """Every thread that talks to Windows audio needs this first."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass


DISCORD_EXES = ("Discord.exe", "DiscordPTB.exe", "DiscordCanary.exe")


def find_discord() -> int | None:
    from .app_audio import find_app_pid
    for exe in DISCORD_EXES:
        pid = find_app_pid(exe)
        if pid:
            return pid
    return None


def run_probe(pid: int, timeout: float = 25.0) -> dict:
    """Check, in a separate process, that Windows lets us hear just this app.
    A separate process means a problem there can never crash Word Counter."""
    import json
    import os
    import subprocess
    import tempfile
    from pathlib import Path
    out = Path(tempfile.gettempdir()) / f"wordcounter-probe-{os.getpid()}-{pid}.json"
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / "main.py")]
    cmd += ["--app-audio-probe", str(pid), str(out)]
    try:
        out.unlink(missing_ok=True)
        subprocess.run(cmd, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rep = json.loads(out.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired:
        rep = {"ok": False, "error": "the check took too long"}
    except Exception as e:
        rep = {"ok": False, "error": f"the check didn't finish ({e})"}
    finally:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
    return rep


class Listener(threading.Thread):
    """Records one sound source ('mic', 'pc' or 'discord') until stopped, feeding a Segmenter."""

    NAMES = {"mic": "your microphone", "pc": "PC sound", "discord": "Discord"}

    def __init__(self, kind: str, on_piece: Callable[[str, np.ndarray], None],
                 on_status: Callable[[str, str], None], on_level: Callable[[str, float], None]):
        super().__init__(daemon=True, name=f"listen-{kind}")
        self.kind = kind
        self.on_piece = on_piece
        self.on_status = on_status
        self.on_level = on_level
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        _com_init()
        warnings.filterwarnings("ignore", message=".*discontinuity.*")
        try:
            import soundcard as sc
        except Exception as e:
            self.on_status(self.kind, f"can't use sound devices ({e})")
            return
        seg = Segmenter(lambda a: self.on_piece(self.kind, a), adaptive=(self.kind == "mic"))
        if self.kind == "discord":
            self._run_discord(seg)
            self.on_status(self.kind, "stopped")
            return
        while not self._halt.is_set():
            try:
                if self.kind == "mic":
                    dev = sc.default_microphone()
                    name = dev.name
                    same = lambda: sc.default_microphone().id == dev.id  # noqa: E731
                else:
                    speaker = sc.default_speaker()
                    dev = sc.get_microphone(id=str(speaker.id), include_loopback=True)
                    name = speaker.name
                    same = lambda: sc.default_speaker().id == speaker.id  # noqa: E731
                log.info("listening to %s: %s", self.kind, name)
                self.on_status(self.kind, f"listening ({name})")
                with dev.recorder(samplerate=RATE, channels=1, blocksize=1024) as rec:
                    checks = 0
                    while not self._halt.is_set():
                        data = rec.record(numframes=FRAME)
                        frame = np.asarray(data, dtype=np.float32).reshape(-1).copy()
                        seg.add(frame)
                        self.on_level(self.kind, float(np.abs(frame).max()) if frame.size else 0.0)
                        checks += 1
                        if checks % 30 == 0 and not same():
                            log.info("%s device changed - switching", self.kind)
                            break
            except Exception as e:
                log.warning("listener %s: %s", self.kind, e)
                msg = "no microphone found" if self.kind == "mic" else "can't hear the PC's sound"
                self.on_status(self.kind, f"{msg} - retrying… ({e})")
                self._halt.wait(3)
        self.on_status(self.kind, "stopped")

    def _run_discord(self, seg: "Segmenter") -> None:
        from .app_audio import AppAudio
        checked: dict[int, dict] = {}
        while not self._halt.is_set():
            pid = find_discord()
            if not pid:
                self.on_status(self.kind, "waiting for Discord to open…")
                self._halt.wait(3)
                continue
            if pid not in checked:
                self.on_status(self.kind, "connecting…")
                checked[pid] = run_probe(pid)
                log.info("Discord-only check: %s", checked[pid])
            if not checked[pid].get("ok"):
                self.on_status(self.kind, "Windows won't let me hear Discord by itself "
                               f"({checked[pid].get('error')}). Try “PC sound” instead.")
                self._halt.wait(5)
                continue
            try:
                with AppAudio(pid) as src:
                    self.on_status(self.kind, "listening (Discord only)")
                    checks = 0
                    while not self._halt.is_set():
                        frame = src.read(FRAME)
                        seg.add(frame)
                        self.on_level(self.kind, float(np.abs(frame).max()) if frame.size else 0.0)
                        checks += 1
                        if checks % 50 == 0 and find_discord() != pid:
                            log.info("Discord restarted or closed")
                            break
            except Exception as e:
                log.warning("Discord listener: %s", e)
                self.on_status(self.kind, f"lost Discord's sound - retrying… ({e})")
                self._halt.wait(3)
