"""Speech recognition that runs on your own PC (Whisper - no internet needed once a model is there).

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


def load_model(path=None, gpu: bool = False):
    import os
    from faster_whisper import WhisperModel
    path = path or model_dir()
    if not (path / "model.bin").exists():
        raise RuntimeError(f"speech model missing at {path}")
    if gpu:
        model = WhisperModel(str(path), device="cuda", compute_type="float16")
        # run one tiny bit of silence so any missing graphics-card library shows up now, not mid-game
        segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1,
                                       without_timestamps=True)
        list(segments)
        return model
    threads = max(2, min(8, (os.cpu_count() or 4) // 2))
    return WhisperModel(str(path), device="cpu", compute_type="int8", cpu_threads=threads)


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
    """Background worker: load a model, then transcribe pieces as they arrive.
    switch(path) loads a different model without stopping."""

    def __init__(self, on_text: Callable[[str, str], None], on_ready: Callable[[str | None], None],
                 get_hotwords: Callable[[], str], model_path=None, use_gpu: bool = False,
                 on_switch_failed: Callable[[str, str], None] = lambda msg, what: None):
        super().__init__(daemon=True, name="recognizer")
        self.on_text = on_text
        self.on_ready = on_ready
        self.get_hotwords = get_hotwords
        self.model_path = model_path
        self.on_switch_failed = on_switch_failed
        self.use_gpu = use_gpu
        self.on_gpu = False
        self.q: queue.Queue = queue.Queue()
        self.model = None

    def submit(self, source: str, audio: np.ndarray) -> None:
        # if the PC is falling behind, drop the oldest pieces so counts stay live
        while self.q.qsize() > 6:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "__switch__":       # never drop a model change
                self.q.put(item)
                break
        self.q.put((source, audio))

    def switch(self, path, gpu: bool) -> None:
        self.q.put(("__switch__", (path, gpu)))

    def _load(self, path, gpu: bool) -> str | None:
        """Load a model; returns None if it worked, otherwise the reason."""
        t = time.monotonic()
        try:
            self.model = load_model(path, gpu)
        except Exception as e:
            log.warning("speech model failed to load (%s)", "graphics card" if gpu else "processor",
                        exc_info=True)
            return str(e) or type(e).__name__
        self.on_gpu = gpu
        log.info("speech model %s loaded on the %s in %.1f s", getattr(path, "name", "base.en"),
                 "graphics card" if gpu else "processor", time.monotonic() - t)
        return None

    def _load_best(self, path, gpu: bool) -> str | None:
        """Try graphics card -> processor -> built-in model, reporting what had to give way."""
        err = self._load(path, gpu)
        if err and gpu:
            from .gpu import describe_error
            gpu_err = describe_error(RuntimeError(err))
            err = self._load(path, False)
            if not err:
                self.on_switch_failed(f"The graphics card didn't work ({gpu_err}), "
                                      "so it's using the processor instead.", "gpu")
                return None
        if err and path is not None:
            first = err
            err = self._load(None, False)
            if not err:
                self.on_switch_failed(f"That accuracy model wouldn't load ({first}), using Fast instead.",
                                      "model")
                return None
        return err

    def run(self) -> None:
        err = self._load_best(self.model_path, self.use_gpu)
        self.on_ready(None if not err else f"speech engine failed to start ({err})")
        while True:
            source, audio = self.q.get()
            if source == "__switch__":
                path, gpu = audio
                old, old_gpu = self.model, self.on_gpu
                self.model = None
                err = self._load_best(path, gpu)
                if err:
                    self.model, self.on_gpu = old, old_gpu
                    self.on_switch_failed(err, "model")
                self.on_ready(None)
                continue
            if audio is None:
                return
            if self.model is None:
                continue
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
