"""Word Counter - start here.

Usage:
  WordCounter.exe               open the app
  WordCounter.exe --self-test   check that everything works and show a report

Developer / build-check options:
  --report FILE                       write the --self-test report to FILE
  --screenshot FILE --quit-after SEC  save a picture of the window (with example words)
  --hear-test WAV REPORT --words "bro,no way"
                                      run a sound file through the speech engine and count words
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from logging.handlers import RotatingFileHandler


def setup_logging():
    from wordcounter.config import app_data_dir
    handlers = [RotatingFileHandler(app_data_dir() / "app.log", maxBytes=1_000_000,
                                    backupCount=2, encoding="utf-8")]
    if sys.stderr is not None:          # the windowed .exe has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def self_test(report: str | None) -> int:
    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            extra = fn()
            lines.append(f"✔ {name}" + (f" - {extra}" if extra else ""))
        except Exception as e:
            ok = False
            lines.append(f"✘ {name} - {e}")

    from wordcounter import APP_VERSION
    lines.append(f"Word Counter {APP_VERSION}")
    check("PySide6", lambda: __import__("PySide6").__version__)
    check("numpy", lambda: __import__("numpy").__version__)
    check("soundcard", lambda: (__import__("soundcard") and "loaded"))
    check("faster-whisper", lambda: __import__("faster_whisper").__version__)

    def discord():
        from wordcounter.audio import find_discord
        pid = find_discord()
        return f"Discord is running (process {pid})" if pid else "Discord isn't open right now"
    check("Discord-only listening", discord)

    def model():
        from wordcounter.recognizer import load_model, model_dir
        t = time.monotonic()
        load_model()
        return f"{model_dir().name} loaded in {time.monotonic() - t:.1f} s"
    check("Speech model", model)

    def devices():
        import soundcard as sc
        mics = [m.name for m in sc.all_microphones()]
        spk = [s.name for s in sc.all_speakers()]
        return f"{len(mics)} microphone(s), {len(spk)} speaker(s)"
    try:
        lines.append("ℹ Sound devices - " + devices())
    except Exception as e:
        lines.append(f"ℹ Sound devices - none found ({e})")

    text = "\n".join(lines)
    if report:
        with open(report, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information(None, "Word Counter self-test", text)
    return 0 if ok else 1


def hear_test(wav: str, report: str, words: list[str]) -> int:
    """Feed a .wav file through the same path as live listening and count the words."""
    import wave
    import numpy as np
    from wordcounter import matching
    from wordcounter.audio import FRAME, RATE, Segmenter
    from wordcounter.recognizer import load_model, transcribe

    with wave.open(wav, "rb") as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2, \
            "test file must be 16 kHz mono 16-bit"
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    audio = np.concatenate([audio, np.zeros(RATE, dtype=np.float32)])   # trailing quiet ends the last piece
    pieces = []
    seg = Segmenter(pieces.append, adaptive=True)
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        seg.add(audio[i:i + FRAME])
    t = time.monotonic()
    model = load_model()
    load_s = time.monotonic() - t
    texts, counts = [], {w: 0 for w in words}
    t = time.monotonic()
    for p in pieces:
        text = transcribe(model, p, ", ".join(words))
        texts.append(text)
        for w in words:
            counts[w] += matching.count(text, w)
    rep = {"pieces": len(pieces), "texts": texts, "counts": counts,
           "load_s": round(load_s, 2), "hear_s": round(time.monotonic() - t, 2),
           "audio_s": round(len(audio) / RATE, 1)}
    with open(report, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--report")
    ap.add_argument("--screenshot")
    ap.add_argument("--quit-after", type=float)
    ap.add_argument("--hear-test", nargs=2, metavar=("WAV", "REPORT"))
    ap.add_argument("--words", default="")
    ap.add_argument("--app-audio-probe", nargs=2, metavar=("PID", "OUT"))
    args, _ = ap.parse_known_args()

    setup_logging()
    log = logging.getLogger("main")

    if args.app_audio_probe:                  # run by the Discord listener in a separate process
        from wordcounter.app_audio import probe
        pid, out = args.app_audio_probe
        rep = probe(int(pid))
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh)
        return 0
    if args.self_test:
        return self_test(args.report)
    if args.hear_test:
        return hear_test(args.hear_test[0], args.hear_test[1],
                         [w.strip() for w in args.words.split(",") if w.strip()])

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from wordcounter.config import Settings
    from wordcounter.ui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Word Counter")

    if args.screenshot:                       # example window for the build check
        s = Settings(words=[{"text": "bro", "count": 12}, {"text": "no way", "count": 3},
                            {"text": "skibidi", "count": 7}], beep=True)
        s.save = lambda: None                 # don't touch the real settings
        win = MainWindow(s, start_engine=False)
        win.on_ready(None)
        win.transcript = [("mic", "Bro, no way you just did that."),
                          ("mic", "That was so skibidi, bro.")]
        win.render_transcript()
        win.show()

        def shot():
            win.grab().save(args.screenshot)
            app.quit()
        QTimer.singleShot(int((args.quit_after or 2) * 1000), shot)
        return app.exec()

    win = MainWindow(Settings.load())
    win.show()
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), win.close)
    log.info("Word Counter started")
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logging.getLogger("main").error("crash:\n%s", traceback.format_exc())
        raise
