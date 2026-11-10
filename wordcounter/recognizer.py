"""Speech recognition that runs on your own PC (Whisper "base.en", no internet needed).

Plain English: pieces of speech go into a queue; one background worker turns
each piece into text and reports it back.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from .config import resource_dir

log = logging.getLogger(__name__)

MODEL_NAME = "base.en"


def model_dir() -> Path:
    return resource_dir() / "models" / MODEL_NAME


def load_model():
    from faster_whisper import WhisperModel
    path = model_dir()
    if not (path / "model.bin").exists():
        raise RuntimeError(f"speech model missing at {path}")
    return WhisperModel(str(path), device="cpu", compute_type="int8", cpu_threads=4)


def transcribe(model, audio: np.ndarray, hotwords: str = "") -> str:
    """Audio (16 kHz mono float32) -> text. Skips bits that are probably not speech."""
    kwargs = dict(language="en", beam_size=1, vad_filter=False, without_timestamps=True,
                  condition_on_previous_text=False, temperature=0.0)
    if hotwords:
        kwargs["hotwords"] = hotwords
    try:
        segments, _ = model.transcribe(audio, **kwargs)
    except TypeError:                      # older faster-whisper without hotwords
        kwargs.pop("hotwords", None)
        segments, _ = model.transcribe(audio, **kwargs)
    parts = []
    for s in segments:
        if s.no_speech_prob > 0.6 or s.avg_logprob < -1.2:
            continue
        parts.append(s.text.strip())
    return " ".join(p for p in parts if p)


class Recognizer(threading.Thread):
    """Background worker: load the model once, then transcribe pieces as they arrive."""

    def __init__(self, on_text: Callable[[str, str], None], on_ready: Callable[[str | None], None],
                 get_hotwords: Callable[[], str]):
        super().__init__(daemon=True, name="recognizer")
        self.on_text = on_text
        self.on_ready = on_ready
        self.get_hotwords = get_hotwords
        self.q: queue.Queue = queue.Queue()
        self.model = None

    def submit(self, source: str, audio: np.ndarray) -> None:
        # if the PC is falling behind, drop the oldest pieces so counts stay live
        while self.q.qsize() > 6:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.q.put((source, audio))

    def run(self) -> None:
        t = time.monotonic()
        try:
            self.model = load_model()
        except Exception as e:
            log.exception("speech model failed to load")
            self.on_ready(f"speech engine failed to start ({e})")
            return
        log.info("speech model loaded in %.1f s", time.monotonic() - t)
        self.on_ready(None)
        while True:
            source, audio = self.q.get()
            if audio is None:
                return
            try:
                # no word hints: Whisper tends to skip hinted words at the start of a sentence
                text = transcribe(self.model, audio)
            except Exception:
                log.exception("transcribing failed")
                continue
            if text:
                self.on_text(source, text)

    def stop(self) -> None:
        self.q.put(("", None))
