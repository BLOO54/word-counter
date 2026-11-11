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

    def bot_engine():
        from wordcounter import discord_bot
        why = discord_bot.available()
        if why:
            raise RuntimeError(why)
        return "bot engine included"
    check("Discord bot", bot_engine)

    def graphics():
        from wordcounter import gpu
        return ("NVIDIA driver found" if gpu.has_nvidia_driver() else "no NVIDIA driver") + \
               (", graphics card support downloaded" if gpu.installed() else "")
    check("Graphics card", graphics)

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


def hear_test(wav: str, report: str, words: list[str], model_name: str = "fast",
              live: bool = False, careful: bool = False) -> int:
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
    pieces = []                                   # (piece id, audio, final)
    seg = Segmenter(adaptive=True, live=lambda: live,
                    on_piece=lambda pid, a, final: pieces.append((pid, a, final)))
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        seg.add(audio[i:i + FRAME])
    from wordcounter.models import ModelStore
    store = ModelStore()
    t = time.monotonic()
    download_s = 0.0
    if model_name != "fast" and not store.installed(model_name):
        store.download(model_name)
        download_s = time.monotonic() - t
    t = time.monotonic()
    model = load_model(store.path(model_name) if model_name != "fast" else None)
    load_s = time.monotonic() - t
    from wordcounter.stats import LiveCounter
    lc = LiveCounter()
    texts, counts, early = [], {w: 0 for w in words}, {w: 0 for w in words}
    t = time.monotonic()
    for pid, p, final in pieces:
        text = transcribe(model, p, beam=5 if careful else 1)
        if final and text:
            texts.append(text)
        for w, d in lc.update("test", pid, text, final, words).items():
            counts[w] += d
            if not final and d > 0:
                early[w] += d
    rep = {"model": model_name, "live": live, "careful": careful, "download_s": round(download_s, 1),
           "pieces": sum(1 for x in pieces if x[2]), "live_checks": sum(1 for x in pieces if not x[2]),
           "texts": texts, "counts": counts, "counted_early": early,
           "load_s": round(load_s, 2), "hear_s": round(time.monotonic() - t, 2),
           "audio_s": round(len(audio) / RATE, 1)}
    with open(report, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2)
    return 0


def bench(folder: str, report: str, model_name: str = "better") -> int:
    """Accuracy test: current way vs "catch quiet & slurred words" (with and without word hints)."""
    from pathlib import Path
    import wave
    import numpy as np
    from wordcounter import matching
    from wordcounter.audio import FRAME, Segmenter
    from wordcounter.models import ModelStore
    from wordcounter.recognizer import load_model, transcribe

    folder = Path(folder)
    man = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    words, exp = man["words"], man["expected_per_file"]
    store = ModelStore()
    if model_name != "fast" and not store.installed(model_name):
        store.download(model_name)
    model = load_model(store.path(model_name) if model_name != "fast" else None)
    configs = {"current": (False, False), "new": (True, False), "new+hints": (True, True)}
    rep = {"model": model_name, "expected_per_file": exp, "results": {}}
    for cfg, (sens, hints) in configs.items():
        per_file, tot_caught, tot_exp, tot_extra = {}, 0, 0, 0
        t = time.monotonic()
        for f in man["files"]:
            with wave.open(str(folder / f["file"]), "rb") as w:
                audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
            audio = np.concatenate([audio, np.zeros(16000, np.float32)])
            pieces = []
            seg = Segmenter(pieces.append, adaptive=False, sensitive=lambda s=sens: s)
            for i in range(0, len(audio) - FRAME + 1, FRAME):
                seg.add(audio[i:i + FRAME])
            got = {w: 0 for w in words}
            texts = []
            for piece in pieces:
                text = transcribe(model, piece, hotwords=", ".join(words) if hints else "", sensitive=sens)
                texts.append(text)
                for w in words:
                    got[w] += matching.count(text, w, similar=True)
            caught = sum(min(got[w], exp[w]) for w in words)
            extra = sum(max(0, got[w] - exp[w]) for w in words)
            total = sum(exp.values())
            per_file[f["file"]] = {"caught": caught, "of": total, "extra": extra, "got": got,
                                   "pieces": len(pieces), "texts": texts}
            tot_caught, tot_exp, tot_extra = tot_caught + caught, tot_exp + total, tot_extra + extra
        rep["results"][cfg] = {"caught": tot_caught, "of": tot_exp, "pct": round(100 * tot_caught / tot_exp, 1),
                               "wrong_extra_counts": tot_extra, "seconds": round(time.monotonic() - t),
                               "files": per_file}
    with open(report, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2, ensure_ascii=False)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--report")
    ap.add_argument("--screenshot")
    ap.add_argument("--quit-after", type=float)
    ap.add_argument("--hear-test", nargs=2, metavar=("WAV", "REPORT"))
    ap.add_argument("--words", default="")
    ap.add_argument("--model", default="fast")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--careful", action="store_true")
    ap.add_argument("--app-audio-probe", nargs=2, metavar=("PID", "OUT"))
    ap.add_argument("--listen-test", metavar="REPORT")
    ap.add_argument("--gpu-test", metavar="REPORT")
    ap.add_argument("--bench", nargs=2, metavar=("DIR", "REPORT"))
    ap.add_argument("--bot-selftest", metavar="REPORT")
    args, _ = ap.parse_known_args()

    setup_logging()
    log = logging.getLogger("main")

    if args.listen_test:                      # press "Start" without a window, report what each source says
        import threading
        from wordcounter.audio import Listener
        statuses: dict = {}
        lock = threading.Lock()

        def status(kind, text):
            with lock:
                statuses.setdefault(kind, []).append(text)
        crashes = []
        threading.excepthook = lambda a: crashes.append(f"{a.thread.name}: {a.exc_type.__name__}: {a.exc_value}")
        kinds = ["pc", "mic", "discord", "spotify"]
        for kind in kinds:
            Listener(kind, lambda *a: None, status, lambda k, v: None).start()
        time.sleep(6)
        missing = [k for k in kinds if not statuses.get(k)]
        ok = not crashes and not missing and not any(
            "can't use sound devices" in t for ts in statuses.values() for t in ts)
        with open(args.listen_test, "w", encoding="utf-8") as fh:
            json.dump({"ok": ok, "crashes": crashes, "silent": missing, "statuses": statuses},
                      fh, indent=2, ensure_ascii=False)
        return 0
    if args.bench:
        return bench(args.bench[0], args.bench[1], args.model if args.model != "fast" else "better")
    if args.bot_selftest:                     # build check: the bundled bot loads and decodes voice
        from wordcounter import discord_bot
        rep = discord_bot.selftest()
        with open(args.bot_selftest, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
        return 0
    if args.gpu_test:                         # build check: download + load NVIDIA's libraries
        import ctypes
        from wordcounter import gpu
        rep = {}
        t = time.monotonic()
        try:
            gpu.download()
            rep["download_s"] = round(time.monotonic() - t, 1)
            rep["activated"] = gpu.activate()
            info = json.loads((gpu.libs_dir() / "ready.json").read_text(encoding="utf-8"))
            rep["packages"] = [f"{w['package']} {w['version']} ({w['size'] / 1e6:.0f} MB)" for w in info["wheels"]]
            loads = {}
            for name in ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"):
                try:
                    ctypes.WinDLL(name)
                    loads[name] = "ok"
                except OSError as e:
                    loads[name] = str(e)
            rep["dll_loads"] = loads
            rep["total_mb"] = round(sum(f.stat().st_size for f in gpu.libs_dir().iterdir()) / 1e6)
        except Exception as e:
            rep["error"] = repr(e)
        try:
            from wordcounter.recognizer import load_model
            load_model(None, gpu=True)
            rep["gpu_model"] = "loaded"
        except Exception as e:
            rep["gpu_model"] = f"failed as expected without a graphics card: {gpu.describe_error(e)}"
        with open(args.gpu_test, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
        return 0
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
                         [w.strip() for w in args.words.split(",") if w.strip()], args.model,
                         args.live, args.careful)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from wordcounter.config import Settings
    from wordcounter.ui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Word Counter")

    if args.screenshot:                       # example window for the build check
        s = Settings(words=[{"text": "bro", "count": 12, "by": {"1": 7, "2": 4, "3": 1}},
                            {"text": "no way", "count": 3, "by": {"2": 3}},
                            {"text": "skibidi", "count": 7}], beep=True, milestone=25, overlay=True,
                     people={"1": "Connor", "2": "Blade", "3": "tessi"})
        s.save = lambda: None                 # don't touch the real settings
        win = MainWindow(s, start_engine=False)
        win.on_ready(None)
        win.transcript = [{"key": ("mic", 1), "source": "mic", "text": "Bro, no way you just did that.",
                           "final": True},
                          {"key": ("mic", 2), "source": "mic", "text": "That was so skibidi, bro",
                           "final": False}]
        win.render_transcript()
        win.show()

        def shot():
            win.grab().save(args.screenshot)
            from wordcounter.ui import Toast
            t = Toast("🎉 “bro” hit 25!")
            t.show_for(3000)
            from wordcounter.ui import DownloadsDialog
            d = DownloadsDialog(win)
            d.show()
            from wordcounter.ui import BotSetupDialog
            b = BotSetupDialog(win)
            b.show()

            def finish():
                t.grab().save(args.screenshot.replace(".png", "-milestone.png"))
                d.grab().save(args.screenshot.replace(".png", "-downloads.png"))
                win.overlay.grab().save(args.screenshot.replace(".png", "-overlay.png"))
                b.grab().save(args.screenshot.replace(".png", "-bot.png"))
                app.quit()
            QTimer.singleShot(400, finish)
        QTimer.singleShot(int((args.quit_after or 2) * 1000), shot)
        return app.exec()

    from wordcounter import gpu
    gpu.remove_if_marked()
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
