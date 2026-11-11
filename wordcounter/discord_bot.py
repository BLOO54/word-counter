"""The "Discord bot" way of listening: each person's voice arrives separately.

Plain English: Word Counter starts a small helper program (bot/index.js, run by a
bundled copy of Node.js). It logs in as YOUR bot, follows you into voice channels and
sends each person's voice here on its own. Each person then gets their own clean
sentences - no more voices on top of each other.

The bot key ("token") is stored encrypted with Windows' own protection (DPAPI), so only
your Windows account on this PC can read it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .config import resource_dir

log = logging.getLogger(__name__)

RATE = 16000
LIVE_EVERY = int(RATE * 0.8)        # "count while talking": check each person's sentence every 0.8 s
MAX_PIECE = RATE * 6                # very long talking is cut into ~6 s pieces
MIN_PIECE = int(RATE * 0.25)
PERMISSIONS = 1024 | (1 << 20)      # View Channel + Connect (it never needs to talk)


# ------------------------------------------------------------------ finding the helper program

def bot_dir() -> Path:
    return resource_dir() / "bot"


def node_exe() -> str | None:
    bundled = bot_dir() / "node.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("node")


def available() -> str | None:
    """None if the bot can run in this build, otherwise why not."""
    if not (bot_dir() / "index.js").exists():
        return "the Discord bot isn't included in this build"
    if not node_exe():
        return "the bot's engine (Node.js) is missing from this build"
    if not (bot_dir() / "node_modules").exists():
        return "the bot's libraries are missing from this build"
    return None


# ------------------------------------------------------------------ keeping the token safe

def protect(text: str) -> str:
    if not text:
        return ""
    data = text.encode("utf-8")
    if sys.platform == "win32":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(data, encrypt=True)).decode("ascii")
        except Exception:
            log.warning("couldn't encrypt the bot token", exc_info=True)
    return "b64:" + base64.b64encode(data).decode("ascii")


def unprotect(stored: str) -> str:
    try:
        if stored.startswith("dpapi:"):
            return _dpapi(base64.b64decode(stored[6:]), encrypt=False).decode("utf-8")
        if stored.startswith("b64:"):
            return base64.b64decode(stored[4:]).decode("utf-8")
    except Exception:
        log.warning("couldn't read the saved bot token", exc_info=True)
    return ""


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):   # 0x1 = no UI
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def bot_id_from_token(token: str) -> str | None:
    """A bot token starts with the bot's id (base64) - used to make the invite link."""
    try:
        first = token.split(".")[0]
        return base64.b64decode(first + "=" * (-len(first) % 4)).decode("ascii")
    except Exception:
        return None


def invite_url(bot_id: str) -> str:
    return (f"https://discord.com/oauth2/authorize?client_id={bot_id}&scope=bot"
            f"&permissions={PERMISSIONS}")


# ------------------------------------------------------------------ talking to the helper

def read_frames(stream):
    """Yield (kind, payload) from the helper's output: [1 byte kind][4 byte length][payload]."""
    while True:
        head = stream.read(5)
        if not head or len(head) < 5:
            return
        kind, length = head[0], struct.unpack("<I", head[1:5])[0]
        payload = b""
        while len(payload) < length:
            chunk = stream.read(length - len(payload))
            if not chunk:
                return
            payload += chunk
        yield kind, payload


def _start(args: list[str], token: str = "") -> subprocess.Popen:
    env = dict(os.environ)
    if token:
        env["WC_TOKEN"] = token
    return subprocess.Popen([node_exe(), str(bot_dir() / "index.js"), *args], cwd=str(bot_dir()), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _events(proc: subprocess.Popen, timeout: float) -> list[dict]:
    out, deadline = [], time.monotonic() + timeout
    box: list = []
    t = threading.Thread(target=lambda: box.extend(read_frames(proc.stdout)), daemon=True)
    t.start()
    t.join(max(0.0, deadline - time.monotonic()))
    if t.is_alive():
        proc.kill()
        t.join(2)
    for kind, payload in box:
        if kind == 1:
            out.append(json.loads(payload.decode("utf-8")))
    return out


def check_token(token: str, timeout: float = 30) -> dict:
    """Log in once and report {'bot': name, 'id': ..., 'guilds': [...]} or {'error': ...}."""
    why = available()
    if why:
        return {"error": why}
    proc = _start(["--check"], token)
    events = _events(proc, timeout)
    for e in events:
        if e.get("t") in ("check", "error"):
            return e if e["t"] == "check" else {"error": e.get("msg", "unknown problem")}
    err = proc.stderr.read().decode("utf-8", "replace")[-300:] if proc.stderr else ""
    return {"error": "the bot didn't answer in time" + (f" ({err.strip()})" if err.strip() else "")}


def selftest(timeout: float = 60) -> dict:
    why = available()
    if why:
        return {"ok": False, "error": why}
    events = _events(_start(["--selftest"]), timeout)
    return next((e for e in events if e.get("t") == "selftest"), {"ok": False, "error": "no answer"})


# ------------------------------------------------------------------ listening

class BotListener(threading.Thread):
    """Runs the bot while listening. Same callbacks as audio.Listener, but every person is their
    own source: 'bot:<user id>' (names are in self.names)."""

    kind = "bot"

    def __init__(self, token: str, on_piece: Callable[[str, np.ndarray, int, bool], None],
                 on_status: Callable[[str, str], None], on_level: Callable[[str, float], None],
                 live: Callable[[], bool] = lambda: False):
        super().__init__(daemon=True, name="listen-bot")
        self.token = token
        self.on_piece = on_piece
        self.on_status = on_status
        self.on_level = on_level
        self.live = live
        self.names: dict[str, str] = {}
        self.people: dict[str, dict] = {}
        self.proc: subprocess.Popen | None = None
        self._halt = threading.Event()
        self._next_piece = 0

    def stop(self) -> None:
        self._halt.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.close()            # the bot leaves the call and exits
            except Exception:
                pass
            threading.Timer(3.0, lambda: p.poll() is None and p.kill()).start()

    def run(self) -> None:
        why = available()
        if why:
            self.on_status(self.kind, why)
            return
        while not self._halt.is_set():
            self.on_status(self.kind, "starting the bot…")
            try:
                self.proc = _start([], self.token)
            except Exception as e:
                self.on_status(self.kind, f"couldn't start the bot ({e})")
                return
            threading.Thread(target=self._log_stderr, args=(self.proc,), daemon=True).start()
            fatal = False
            for kind, payload in read_frames(self.proc.stdout):
                if kind == 1:
                    fatal = self._event(json.loads(payload.decode("utf-8"))) or fatal
                elif kind == 2:
                    n = payload[0]
                    self._audio(payload[1:1 + n].decode("ascii"), payload[1 + n:])
            for uid in list(self.people):
                self._finish(uid)
            code = self.proc.wait()
            if self._halt.is_set() or fatal or code in (2, 3):
                break
            self.on_status(self.kind, "the bot stopped unexpectedly - restarting…")
            self._halt.wait(5)
        self.on_status(self.kind, "stopped")

    def _log_stderr(self, proc):
        for line in iter(proc.stderr.readline, b""):
            log.info("bot: %s", line.decode("utf-8", "replace").rstrip())

    def _event(self, e: dict) -> bool:
        t = e.get("t")
        if t == "ready":
            self.on_status(self.kind, f"online as {e.get('bot')} - join a voice channel and it will follow you")
        elif t == "joined":
            self.on_status(self.kind, f"in “{e.get('channel')}” ({e.get('guild')}) - hearing everyone separately")
        elif t == "left":
            self.on_status(self.kind, "waiting for you to join a voice channel…")
        elif t == "start":
            self.names[e["id"]] = e.get("name") or "someone"
        elif t == "end":
            self._finish(e["id"])
        elif t == "error":
            self.on_status(self.kind, f"⚠ {e.get('msg')}")
            return "token" in (e.get("msg") or "")
        return False

    def _audio(self, uid: str, raw: bytes) -> None:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if not samples.size:
            return
        p = self.people.get(uid)
        if p is None:
            p = self.people[uid] = {"parts": [], "n": 0, "since_live": 0, "live_sent": False,
                                    "piece": self._new_piece()}
        p["parts"].append(samples)
        p["n"] += samples.size
        p["since_live"] += samples.size
        self.on_level(self.kind, float(np.abs(samples).max()))
        if p["n"] >= MAX_PIECE:
            self._finish(uid, keep=True)
        elif self.live() and p["since_live"] >= LIVE_EVERY and p["n"] >= RATE // 2:
            p["since_live"] = 0
            p["live_sent"] = True
            self.on_piece(f"bot:{uid}", np.concatenate(p["parts"]), p["piece"], False)

    def _finish(self, uid: str, keep: bool = False) -> None:
        p = self.people.pop(uid, None)
        if not p:
            return
        if p["n"] >= MIN_PIECE:
            self.on_piece(f"bot:{uid}", np.concatenate(p["parts"]), p["piece"], True)
        elif p["live_sent"]:
            self.on_piece(f"bot:{uid}", np.zeros(0, np.float32), p["piece"], True)
        if keep:            # still talking - carry on in a fresh piece
            self.people[uid] = {"parts": [], "n": 0, "since_live": 0, "live_sent": False,
                                "piece": self._new_piece()}

    def _new_piece(self) -> int:
        self._next_piece += 1
        return self._next_piece
