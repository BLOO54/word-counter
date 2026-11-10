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
LIVE_EVERY = 8               # with "count while talking": check the sentence so far every 0.8 s
PREROLL = 5                  # keep 0.5 s before speech starts so first words aren't clipped


class Segmenter:
    """Turns a stream of 0.1 s frames into pieces of speech."""

    def __init__(self, emit: Callable[[np.ndarray], None] | None = None, adaptive: bool = True,
                 on_piece: Callable[[int, np.ndarray, bool], None] | None = None,
                 live: Callable[[], bool] = lambda: False, live_every: int = LIVE_EVERY):
        """emit(audio) gets each finished piece. Or on_piece(piece_id, audio, final) gets finished
        pieces (final=True) AND, while live() is true, the piece-so-far every `live_every` frames
        (final=False) - so words can be counted while someone is still talking."""
        self.emit = emit
        self.on_piece = on_piece
        self.live = live
        self.live_every = live_every
        self.piece_id = 0
        self.since_live = 0
        self.live_sent = False
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
        else:
            self.since_live += 1
            if (self.on_piece is not None and self.since_live >= self.live_every and self.speech >= 3
                    and self.quiet < 3 and self.live()):
                self.since_live = 0
                self.live_sent = True
                self.on_piece(self.piece_id, np.concatenate(self.frames), False)

    def _flush(self, upto: int, keep_going: bool = False) -> None:
        piece, rest = self.frames[:upto], self.frames[upto:]
        rest_levels = self.levels[upto:]
        if self.speech >= 3 and piece:
            audio = np.concatenate(piece)
            if self.on_piece is not None:
                self.on_piece(self.piece_id, audio, True)
            elif self.emit is not None:
                self.emit(audio)
        elif self.on_piece is not None and self.live_sent:
            self.on_piece(self.piece_id, np.zeros(0, np.float32), True)   # close a piece that got a live check
        self.piece_id += 1
        self.since_live = 0
        self.live_sent = False
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


APPS = {                      # sources that record ONE app: kind -> (name, its .exe files)
    "discord": ("Discord", DISCORD_EXES),
    "spotify": ("Spotify", ("Spotify.exe",)),
}


def find_app(kind: str) -> int | None:
    from .app_audio import find_app_pid
    for exe in APPS[kind][1]:
        pid = find_app_pid(exe)
        if pid:
            return pid
    return None


def find_discord() -> int | None:
    return find_app("discord")


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
    """Records one sound source ('mic', 'pc', 'discord' or 'spotify') until stopped, feeding a Segmenter."""

    NAMES = {"mic": "your microphone", "pc": "PC sound", "discord": "Discord", "spotify": "Spotify"}

    def __init__(self, kind: str, on_piece: Callable[[str, np.ndarray, int, bool], None],
                 on_status: Callable[[str, str], None], on_level: Callable[[str, float], None],
                 live: Callable[[], bool] = lambda: False):
        super().__init__(daemon=True, name=f"listen-{kind}")
        self.kind = kind
        self.on_piece = on_piece
        self.on_status = on_status
        self.on_level = on_level
        self.live = live
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        warnings.filterwarnings("ignore", message=".*discontinuity.*")
        seg = Segmenter(adaptive=(self.kind == "mic"), live=self.live,
                        on_piece=lambda pid, a, final: self.on_piece(self.kind, a, pid, final))
        if self.kind in APPS or self.kind == "mic":   # these talk to Windows directly (no soundcard)
            (self._run_app if self.kind in APPS else self._run_mic)(seg)
            self.on_status(self.kind, "stopped")
            return
        try:
            # soundcard switches on Windows audio (COM) itself when first loaded, and treats
            # "it's already on" as an error - so it must be loaded BEFORE we do that ourselves.
            import soundcard as sc
        except Exception as e:
            log.exception("soundcard failed to load")
            self.on_status(self.kind, f"can't use sound devices ({e})")
            return
        _com_init()      # a 2nd listener thread (mic + PC sound) still needs COM on its own thread
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
                rec, rate = self._open(dev)
                self.on_status(self.kind, f"listening ({name})")
                with rec as r:
                    need = FRAME * rate // RATE
                    checks = 0
                    while not self._halt.is_set():
                        data = np.asarray(r.record(numframes=need), dtype=np.float32)
                        frame = to_16k_mono(data, rate)
                        seg.add(frame)
                        self.on_level(self.kind, float(np.abs(frame).max()) if frame.size else 0.0)
                        checks += 1
                        if checks % 30 == 0 and not same():
                            log.info("%s device changed - switching", self.kind)
                            break
            except Exception as e:
                log.warning("listener %s failed: %r", self.kind, e, exc_info=True)
                msg = "the microphone won't start" if self.kind == "mic" else "can't hear the PC's sound"
                self.on_status(self.kind, f"{msg} - retrying… ({describe(e)})")
                self._halt.wait(3)
        self.on_status(self.kind, "stopped")

    def _open(self, dev):
        """Open a recorder. Some USB mics refuse 16 kHz mono, so fall back to the mic's own
        format (we convert it ourselves). Returns (opened recorder, its sample rate)."""
        tries = [(RATE, 1), (48000, 1), (48000, None), (44100, None), (44100, 1)]
        last = None
        for rate, ch in tries:
            try:
                kw = {"samplerate": rate, "blocksize": 1024}
                if ch:
                    kw["channels"] = ch
                rec = dev.recorder(**kw)
                rec.__enter__()           # actually opens the device - this is where it can fail
                rec.record(numframes=rate // 100)
            except Exception as e:
                last = e
                log.warning("%s: %s Hz / %s channel(s) refused: %r", self.kind, rate, ch or "all",
                            e, exc_info=True)
                try:
                    rec.__exit__(None, None, None)
                except Exception:
                    pass
                continue
            log.info("%s opened at %s Hz, %s channel(s)", self.kind, rate, ch or "all")
            return _Opened(rec), rate
        raise last if last else RuntimeError("no usable sound format")


    def _run_mic(self, seg: "Segmenter") -> None:
        from .app_audio import MicAudio, default_mic_id
        while not self._halt.is_set():
            try:
                dev_id = default_mic_id()
                with MicAudio() as src:
                    self.on_status(self.kind, "listening")
                    checks = 0
                    while not self._halt.is_set():
                        frame = src.read(FRAME)
                        seg.add(frame)
                        self.on_level(self.kind, float(np.abs(frame).max()) if frame.size else 0.0)
                        checks += 1
                        if checks % 50 == 0 and default_mic_id() != dev_id:
                            log.info("default microphone changed - switching")
                            break
            except Exception as e:
                log.warning("microphone failed: %r", e, exc_info=True)
                self.on_status(self.kind, f"the microphone won't start - retrying… ({describe(e)})")
                self._halt.wait(3)

    def _run_app(self, seg: "Segmenter") -> None:
        """Record just one app (Discord or Spotify), waiting for it to open if needed."""
        from .app_audio import AppAudio
        name = APPS[self.kind][0]
        checked: dict[int, dict] = {}
        while not self._halt.is_set():
            pid = find_app(self.kind)
            if not pid:
                self.on_status(self.kind, f"waiting for {name} to open…")
                self._halt.wait(3)
                continue
            if pid not in checked:
                self.on_status(self.kind, "connecting…")
                checked[pid] = run_probe(pid)
                log.info("%s-only check: %s", name, checked[pid])
            if not checked[pid].get("ok"):
                self.on_status(self.kind, f"Windows won't let me hear {name} by itself "
                               f"({checked[pid].get('error')}). Try “All PC sound” instead.")
                self._halt.wait(5)
                continue
            try:
                with AppAudio(pid) as src:
                    self.on_status(self.kind, f"listening ({name} only)")
                    checks = 0
                    while not self._halt.is_set():
                        frame = src.read(FRAME)
                        seg.add(frame)
                        self.on_level(self.kind, float(np.abs(frame).max()) if frame.size else 0.0)
                        checks += 1
                        if checks % 50 == 0 and find_app(self.kind) != pid:
                            log.info("%s restarted or closed", name)
                            break
            except Exception as e:
                log.warning("%s listener: %s", name, e)
                self.on_status(self.kind, f"lost {name}'s sound - retrying… ({e})")
                self._halt.wait(3)


class _Opened:
    """Wraps an already-opened recorder so `with` closes it without opening it again."""

    def __init__(self, rec):
        self.rec = rec

    def __enter__(self):
        return self.rec

    def __exit__(self, *exc):
        return self.rec.__exit__(*exc)


def describe(e: Exception) -> str:
    return str(e) or type(e).__name__


def to_16k_mono(data: np.ndarray, rate: int) -> np.ndarray:
    """Any channels / sample rate -> 16 kHz mono float32."""
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.reshape(-1).astype(np.float32)
    if rate == RATE:
        return data.copy()
    if rate % RATE == 0:
        k = rate // RATE
        n = len(data) // k * k
        return data[:n].reshape(-1, k).mean(axis=1).astype(np.float32)
    idx = np.arange(0, len(data), rate / RATE)
    return np.interp(idx, np.arange(len(data)), data).astype(np.float32)
