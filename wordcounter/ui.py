"""The Word Counter window."""

from __future__ import annotations

import html
import logging
import sys
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout,
    QWidget,
)

from . import APP_NAME, APP_VERSION, matching, models, updater
from .audio import Listener
from .config import SOURCE_PARTS, Settings, resource_dir
from .recognizer import Recognizer

log = logging.getLogger(__name__)

ORANGE, PINK = "#F97316", "#EC4899"
GRADIENT = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ORANGE}, stop:1 {PINK})"

STYLE = f"""
QWidget {{ background: #0f1115; color: #e8eaf0; font-family: 'Segoe UI'; font-size: 14px; }}
QLabel#title {{ font-size: 24px; font-weight: 700; }}
QLabel#muted, QCheckBox {{ color: #8b93a7; }}
QLineEdit, QComboBox {{ background: #181b22; border: 1px solid #2a2f3a; border-radius: 8px;
    padding: 8px 10px; font-size: 15px; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {ORANGE}; }}
QComboBox QAbstractItemView {{ background: #181b22; selection-background-color: #2a2f3a; }}
QPushButton {{ background: #2a2f3a; border: none; border-radius: 8px; padding: 8px 14px; font-weight: 600; }}
QPushButton:hover {{ background: #353b48; }}
QPushButton#primary {{ background: {GRADIENT}; color: white; }}
QPushButton#listen {{ background: {GRADIENT}; color: white; font-size: 17px; padding: 12px; }}
QPushButton#listen[on="true"] {{ background: #e5484d; }}
QPushButton#small {{ padding: 4px 9px; font-size: 13px; }}
QPushButton#remove {{ background: transparent; color: #8b93a7; font-size: 16px; padding: 0 6px; }}
QPushButton#remove:hover {{ color: #ff5b6b; }}
QFrame#card {{ background: #181b22; border: 1px solid #2a2f3a; border-radius: 12px; }}
QFrame#card[hit="true"] {{ border: 2px solid {PINK}; background: #22161f; }}
QLabel#count {{ font-size: 46px; font-weight: 700; background: transparent; }}
QLabel#word {{ font-size: 17px; font-weight: 600; background: transparent; }}
QTextEdit {{ background: #181b22; border: 1px solid #2a2f3a; border-radius: 8px; padding: 6px;
    color: #aab1c3; font-size: 14px; }}
QProgressBar {{ background: #181b22; border: none; border-radius: 3px; max-height: 6px; }}
QProgressBar::chunk {{ background: {GRADIENT}; border-radius: 3px; }}
QScrollArea {{ border: none; }}
"""

SOURCE_LABELS = [
    ("mic", "🎤  My microphone"),
    ("discord_mic", "💬+🎤  Discord + my microphone"),
    ("discord", "💬  Discord only"),
    ("pc", "🔊  All PC sound (Discord, games, videos)"),
    ("both", "🔊+🎤  All PC sound + my microphone"),
]
SOURCE_TAGS = {"mic": "🎤 ", "discord": "💬 ", "pc": "🔊 "}


class Bridge(QObject):
    """Lets the background threads talk to the window safely."""
    heard = Signal(str, str)
    ready = Signal(object)
    status = Signal(str, str)
    level = Signal(str, float)
    update_result = Signal(object, bool)
    update_progress = Signal(float)
    update_downloaded = Signal(object)
    model_progress = Signal(float)
    model_done = Signal(str, object)
    switch_failed = Signal(str)


class WordCard(QFrame):
    def __init__(self, win: "MainWindow", index: int, text: str, count: int):
        super().__init__()
        self.setObjectName("card")
        self.setMinimumWidth(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 12)
        top = QHBoxLayout()
        name = QLabel(text)
        name.setObjectName("word")
        name.setWordWrap(True)
        rm = QPushButton("✕")
        rm.setObjectName("remove")
        rm.setToolTip("Remove this word")
        rm.clicked.connect(lambda: win.remove_word(index))
        top.addWidget(name, 1)
        top.addWidget(rm, 0, Qt.AlignTop)
        lay.addLayout(top)
        self.count = QLabel(str(count))
        self.count.setObjectName("count")
        lay.addWidget(self.count)
        row = QHBoxLayout()
        for label, fn in (("−1", lambda: win.change(index, -1)), ("+1", lambda: win.change(index, 1)),
                          ("Reset", lambda: win.change(index, None))):
            b = QPushButton(label)
            b.setObjectName("small")
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

    def flash(self):
        self.setProperty("hit", True)
        self.style().unpolish(self)
        self.style().polish(self)
        QTimer.singleShot(450, self._unflash)

    def _unflash(self):
        try:
            self.setProperty("hit", False)
            self.style().unpolish(self)
            self.style().polish(self)
        except RuntimeError:       # card was rebuilt in the meantime
            pass


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, start_engine: bool = True):
        super().__init__()
        self.s = settings
        self.listeners: dict[str, Listener] = {}
        self.listening = False
        self.engine_ready = False
        self.engine_error: str | None = None
        self.transcript: list[tuple[str, str]] = []
        self.source_status: dict[str, str] = {}
        self.cards: list[WordCard] = []
        self._lock = threading.Lock()
        self.update_info = None
        self._update_busy = False
        self.store = models.ModelStore()
        self.model_note = ""
        self.downloading = None

        self.setWindowTitle(APP_NAME)
        icon = resource_dir() / "assets" / "icon.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.setStyleSheet(STYLE)
        self.resize(720, 760)
        self._build()
        self.apply_on_top(self.s.always_on_top, save=False)
        if self.s.geometry:
            try:
                self.restoreGeometry(bytes.fromhex(self.s.geometry))
            except Exception:
                pass

        self.bridge = Bridge()
        self.bridge.heard.connect(self.on_heard)
        self.bridge.ready.connect(self.on_ready)
        self.bridge.status.connect(self.on_status)
        self.bridge.level.connect(self.on_level)
        self.bridge.update_result.connect(self.on_update_result)
        self.bridge.update_progress.connect(self.on_update_progress)
        self.bridge.update_downloaded.connect(self.on_update_downloaded)
        self.recognizer = None
        self.bridge.model_progress.connect(self.on_model_progress)
        self.bridge.model_done.connect(self.on_model_done)
        self.bridge.switch_failed.connect(self.on_switch_failed)
        if start_engine:
            acc = self.s.accuracy
            path = self.store.path(acc) if acc != "fast" and self.store.installed(acc) else None
            if path is None:
                self.s.accuracy = "fast"
                self.set_accuracy_box("fast")
            self.recognizer = Recognizer(self.bridge.heard.emit, self.bridge.ready.emit, self.hotwords,
                                         model_path=path, on_switch_failed=self.bridge.switch_failed.emit)
            self.recognizer.start()
            # quietly look for a new version a few seconds after start, then every few hours
            QTimer.singleShot(4000, lambda: self.check_updates(quiet=True))
            self.update_timer = QTimer(self)
            self.update_timer.timeout.connect(lambda: self.check_updates(quiet=True))
            self.update_timer.start(updater.CHECK_EVERY_HOURS * 3600 * 1000)
        self.update_status()

    # ------------------------------------------------------------------ layout
    def _build(self):
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)

        title = QLabel("Word Counter")
        title.setObjectName("title")
        sub = QLabel("Add a word, press Start, and every time it's said the counter goes up.")
        sub.setObjectName("muted")
        lay.addWidget(title)
        lay.addWidget(sub)

        add = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText('Type a word or phrase, e.g. "bro" or "no way"')
        self.input.returnPressed.connect(self.add_word)
        add_btn = QPushButton("Add")
        add_btn.setObjectName("primary")
        add_btn.clicked.connect(self.add_word)
        add.addWidget(self.input, 1)
        add.addWidget(add_btn)
        lay.addLayout(add)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)
        scroll.setMinimumHeight(170)
        lay.addWidget(scroll, 3)

        src = QHBoxLayout()
        lbl = QLabel("Listen to:")
        lbl.setObjectName("muted")
        self.source = QComboBox()
        for key, text in SOURCE_LABELS:
            self.source.addItem(text, key)
        self.source.setCurrentIndex([k for k, _ in SOURCE_LABELS].index(self.s.source))
        self.source.currentIndexChanged.connect(self.source_changed)
        src.addWidget(lbl)
        src.addWidget(self.source, 1)
        acc_lbl = QLabel("Accuracy:")
        acc_lbl.setObjectName("muted")
        self.accuracy = QComboBox()
        self.fill_accuracy_box()
        self.accuracy.currentIndexChanged.connect(self.accuracy_changed)
        src.addSpacing(10)
        src.addWidget(acc_lbl)
        src.addWidget(self.accuracy)
        lay.addLayout(src)

        self.listen_btn = QPushButton("▶  Start Listening")
        self.listen_btn.setObjectName("listen")
        self.listen_btn.clicked.connect(self.toggle_listening)
        lay.addWidget(self.listen_btn)

        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        lay.addWidget(self.meter)
        self.status = QLabel()
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.heard_label = QLabel("WHAT I'M HEARING")
        self.heard_label.setObjectName("muted")
        lay.addWidget(self.heard_label)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(110)
        lay.addWidget(self.text, 2)
        self.spacer = QWidget()
        lay.addWidget(self.spacer, 2)

        bottom = QHBoxLayout()
        self.beep_box = QCheckBox("Beep when counted")
        self.beep_box.setChecked(self.s.beep)
        self.beep_box.toggled.connect(self.beep_changed)
        self.top_box = QCheckBox("Always on top")
        self.top_box.setChecked(self.s.always_on_top)
        self.top_box.toggled.connect(self.apply_on_top)
        self.text_box = QCheckBox("Show what I'm hearing")
        self.text_box.setChecked(self.s.show_text)
        self.text_box.toggled.connect(self.show_text_changed)
        self.similar_box = QCheckBox("Count similar spellings (Connor / Conner)")
        self.similar_box.setChecked(self.s.similar)
        self.similar_box.toggled.connect(self.similar_changed)
        reset = QPushButton("Reset all counts")
        reset.clicked.connect(self.reset_all)
        clear = QPushButton("Clear text")
        clear.clicked.connect(self.clear_text)
        self.clear_btn = clear
        options = QGridLayout()
        options.setHorizontalSpacing(18)
        options.addWidget(self.beep_box, 0, 0)
        options.addWidget(self.top_box, 0, 1)
        options.addWidget(self.text_box, 1, 0)
        options.addWidget(self.similar_box, 1, 1)
        bottom.addLayout(options)
        bottom.addStretch(1)
        bottom.addWidget(reset)
        bottom.addWidget(clear)
        lay.addLayout(bottom)

        foot = QHBoxLayout()
        ver = QLabel(f"v{APP_VERSION} · speech recognition runs on your PC, nothing is uploaded")
        ver.setObjectName("muted")
        ver.setStyleSheet("font-size: 11px;")
        self.update_btn = QPushButton("⟳  Check for updates")
        self.update_btn.setObjectName("small")
        self.update_btn.clicked.connect(self.update_clicked)
        foot.addWidget(ver, 1)
        foot.addWidget(self.update_btn)
        lay.addLayout(foot)

        self.setCentralWidget(root)
        self.rebuild_cards()
        self.render_transcript()
        self.show_text_changed(self.s.show_text, save=False)

    # ------------------------------------------------------------------ words
    def hotwords(self) -> str:
        return ", ".join(w["text"] for w in self.s.words)

    def rebuild_cards(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.cards = []
        if not self.s.words:
            empty = QLabel("No words yet — type one above and press Add.")
            empty.setObjectName("muted")
            empty.setAlignment(Qt.AlignCenter)
            self.grid.addWidget(empty, 0, 0)
            return
        cols = 3
        for i, w in enumerate(self.s.words):
            card = WordCard(self, i, w["text"], w["count"])
            self.cards.append(card)
            self.grid.addWidget(card, i // cols, i % cols)
        self.grid.setRowStretch(len(self.s.words) // cols + 1, 1)

    def add_word(self):
        text = self.input.text().strip()
        self.input.clear()
        if not matching.words_of(text):
            return
        if any(matching.words_of(w["text"]) == matching.words_of(text) for w in self.s.words):
            return
        self.s.words.append({"text": text, "count": 0})
        self.s.save()
        self.rebuild_cards()
        self.render_transcript()

    def remove_word(self, i: int):
        if 0 <= i < len(self.s.words):
            self.s.words.pop(i)
            self.s.save()
            self.rebuild_cards()
            self.render_transcript()

    def change(self, i: int, delta: int | None):
        if not 0 <= i < len(self.s.words):
            return
        w = self.s.words[i]
        w["count"] = 0 if delta is None else max(0, w["count"] + delta)
        self.cards[i].count.setText(str(w["count"]))
        self.s.save()

    def reset_all(self):
        for w in self.s.words:
            w["count"] = 0
        self.s.save()
        self.rebuild_cards()

    # ------------------------------------------------------------------ hearing
    def on_heard(self, source: str, text: str):
        if self.s.show_text:
            self.transcript.append((source, text))
            self.transcript = self.transcript[-60:]
        hit = False
        for i, w in enumerate(self.s.words):
            n = matching.count(text, w["text"], self.s.similar)
            if n:
                w["count"] += n
                hit = True
                if i < len(self.cards):
                    self.cards[i].count.setText(str(w["count"]))
                    self.cards[i].flash()
        if hit:
            log.info("counted from %s: %s", source,
                     ", ".join(f"{w['text']}={w['count']}" for w in self.s.words))
            self.s.save()
            if self.s.beep:
                self.beep()
        if self.s.show_text:
            self.render_transcript()

    def render_transcript(self):
        phrases = [w["text"] for w in self.s.words]
        several = len(SOURCE_PARTS[self.s.source]) > 1
        out = []
        for source, text in self.transcript:
            pieces, last = [], 0
            for a, b in matching.highlight_spans(text, phrases, self.s.similar):
                pieces.append(html.escape(text[last:a]))
                pieces.append(f'<span style="color:{PINK}; font-weight:700;">{html.escape(text[a:b])}</span>')
                last = b
            pieces.append(html.escape(text[last:]))
            tag = SOURCE_TAGS.get(source, "") if several else ""
            out.append(f"<div style='margin-bottom:4px'>{tag}{''.join(pieces)}</div>")
        if not out:
            out = ["<span style='color:#5d6476'>Words you say will show up here…</span>"]
        self.text.setHtml("".join(out))
        bar = self.text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear_text(self):
        self.transcript = []
        self.render_transcript()

    def beep(self):
        if sys.platform == "win32":
            def go():
                try:
                    import winsound
                    winsound.Beep(880, 110)
                except Exception:
                    pass
            threading.Thread(target=go, daemon=True).start()
        else:
            QApplication.beep()

    # ------------------------------------------------------------------ listening
    def wanted_sources(self) -> list[str]:
        return SOURCE_PARTS[self.s.source]

    def toggle_listening(self):
        if self.listening:
            self.stop_listening()
        else:
            self.start_listening()

    def start_listening(self):
        self.listening = True
        self.source_status = {}
        for kind in self.wanted_sources():
            lst = Listener(kind, self._piece, self.bridge.status.emit, self.bridge.level.emit)
            self.listeners[kind] = lst
            lst.start()
        self.listen_btn.setText("■  Stop Listening")
        self.listen_btn.setProperty("on", True)
        self._restyle(self.listen_btn)
        self.update_status()

    def stop_listening(self):
        self.listening = False
        for lst in self.listeners.values():
            lst.stop()
        self.listeners = {}
        self.source_status = {}
        self.meter.setValue(0)
        self.listen_btn.setText("▶  Start Listening")
        self.listen_btn.setProperty("on", False)
        self._restyle(self.listen_btn)
        self.update_status()

    def _piece(self, source, audio):
        if self.recognizer is not None:
            self.recognizer.submit(source, audio)

    def source_changed(self, _i):
        self.s.source = self.source.currentData()
        self.s.save()
        if self.listening:
            self.stop_listening()
            self.start_listening()
        self.render_transcript()

    def on_ready(self, error):
        if error is None:
            self.model_note = ""
        self.engine_ready = error is None
        self.engine_error = error
        self.update_status()

    def on_status(self, kind, text):
        if kind in self.listeners:
            self.source_status[kind] = text
            self.update_status()

    def on_level(self, kind, value):
        if self.listening:
            self.meter.setValue(min(100, int(value * 160)))

    def update_status(self):
        if self.engine_error:
            self.status.setText(f"⚠ {self.engine_error}")
            return
        parts = []
        if self.model_note:
            parts.append(self.model_note)
        elif not self.engine_ready:
            parts.append("Loading speech engine…")
        if self.listening:
            for kind in self.wanted_sources():
                parts.append(f"{Listener.NAMES[kind].capitalize()}: {self.source_status.get(kind, 'starting…')}")
        elif self.engine_ready:
            parts.append("Ready. Press Start Listening.")
        self.status.setText("   ·   ".join(parts))

    # ------------------------------------------------------------------ options
    def show_text_changed(self, on, save=True):
        on = bool(on)
        for w in (self.heard_label, self.text, self.clear_btn):
            w.setVisible(on)
        self.spacer.setVisible(not on)
        if not on:
            self.transcript = []          # nothing heard is kept while it's switched off
            self.render_transcript()
        if save:
            self.s.show_text = on
            self.s.save()

    def similar_changed(self, on):
        self.s.similar = bool(on)
        self.s.save()
        self.render_transcript()

    def beep_changed(self, on):
        self.s.beep = on
        self.s.save()

    def apply_on_top(self, on, save=True):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        if save:
            self.s.always_on_top = bool(on)
            self.s.save()
            self.show()

    # ------------------------------------------------------------------ accuracy (speech model)
    def fill_accuracy_box(self):
        self.accuracy.blockSignals(True)
        self.accuracy.clear()
        icons = {"fast": "⚡", "better": "🎯", "best": "🏆"}
        for key in models.ORDER:
            repo, label, size, about = models.MODELS[key]
            extra = "" if repo is None or self.store.installed(key) else f"  ({size} download)"
            self.accuracy.addItem(f"{icons[key]}  {label}{extra}", key)
            self.accuracy.setItemData(self.accuracy.count() - 1, about, Qt.ToolTipRole)
        self.accuracy.setCurrentIndex(list(models.ORDER).index(self.s.accuracy))
        self.accuracy.blockSignals(False)

    def set_accuracy_box(self, key):
        self.accuracy.blockSignals(True)
        self.accuracy.setCurrentIndex(list(models.ORDER).index(key))
        self.accuracy.blockSignals(False)

    def accuracy_changed(self, _i):
        from PySide6.QtWidgets import QMessageBox
        key = self.accuracy.currentData()
        if key == self.s.accuracy:
            return
        if self.downloading:
            self.set_accuracy_box(self.s.accuracy)
            return
        if key == "fast" or self.store.installed(key):
            self.use_model(key)
            return
        repo, label, size, about = models.MODELS[key]
        box = QMessageBox(QMessageBox.Question, f"Download {label}?",
                          f"{label} accuracy needs a one-time download ({size}).\n\n"
                          f"It {about}. It runs on your PC like the built-in one.\n\nDownload it now?",
                          QMessageBox.Yes | QMessageBox.No, self)
        box.button(QMessageBox.Yes).setText("Download")
        box.button(QMessageBox.No).setText("Cancel")
        if box.exec() != QMessageBox.Yes:
            self.set_accuracy_box(self.s.accuracy)
            return
        self.downloading = key
        self.accuracy.setEnabled(False)
        self.update_status()

        def work():
            try:
                self.store.download(key, progress=lambda d, t: self.bridge.model_progress.emit(d / t if t else 0))
                self.bridge.model_done.emit(key, None)
            except Exception as e:
                log.exception("model download failed")
                self.bridge.model_done.emit(key, e)
        threading.Thread(target=work, daemon=True).start()

    def on_model_progress(self, frac):
        if self.downloading:
            label = models.MODELS[self.downloading][1]
            self.model_note = f"Downloading {label} accuracy… {int(frac * 100)}%"
            self.update_status()

    def on_model_done(self, key, error):
        self.downloading = None
        self.model_note = ""
        self.accuracy.setEnabled(True)
        if error is not None:
            self.set_accuracy_box(self.s.accuracy)
            self.update_status()
            self.popup("Download failed", f"{error}\n\nCheck your internet and try again.")
            return
        self.fill_accuracy_box()
        self.use_model(key)

    def use_model(self, key):
        self._prev_accuracy = self.s.accuracy
        self.s.accuracy = key
        self.s.save()
        self.set_accuracy_box(key)
        if self.recognizer is not None:
            self.engine_ready = False
            self.model_note = f"Switching to {models.MODELS[key][1]} accuracy…"
            self.update_status()
            self.recognizer.switch(self.store.path(key) if key != "fast" else None)

    def on_switch_failed(self, msg):
        self.engine_ready = True
        self.model_note = ""
        self.s.accuracy = getattr(self, "_prev_accuracy", "fast")
        self.s.save()
        self.set_accuracy_box(self.s.accuracy)
        self.update_status()
        self.popup("Couldn't change accuracy", msg)

    # ------------------------------------------------------------------ updates
    def check_updates(self, quiet: bool):
        """Ask GitHub for a newer version in the background."""
        if self._update_busy:
            return
        self._update_busy = True
        if not quiet:
            self.update_btn.setText("Checking…")
            self.update_btn.setEnabled(False)

        def work():
            try:
                self.bridge.update_result.emit(updater.check_latest(), quiet)
            except Exception as e:
                self.bridge.update_result.emit(e, quiet)
        threading.Thread(target=work, daemon=True).start()

    def on_update_result(self, result, quiet: bool):
        self._update_busy = False
        self.update_btn.setEnabled(True)
        if isinstance(result, Exception):
            log.info("update check failed: %s", result)
            self.update_btn.setText("⟳  Check for updates")
            if not quiet:
                self.popup("Couldn't check for updates", f"{result}\n\nCheck your internet and try again.")
            return
        if result is None:
            self.update_btn.setText("✔  Up to date")
            QTimer.singleShot(4000, lambda: self.update_btn.setText("⟳  Check for updates")
                              if self.update_info is None else None)
            return
        self.update_info = result
        self.update_btn.setText(f"⬆  Update to {result.version}")
        self.update_btn.setObjectName("primary")
        self._restyle(self.update_btn)
        if not quiet:
            self.offer_update()

    def update_clicked(self):
        if self.update_info is None:
            self.check_updates(quiet=False)
        else:
            self.offer_update()

    def offer_update(self):
        from PySide6.QtWidgets import QMessageBox
        info = self.update_info
        if updater.install_mode() != "installed":
            self.popup("Update available",
                       f"Version {info.version} is out, but this copy wasn't installed with the "
                       f"installer, so it can't update itself.\n\nDownload it here:\n{info.page_url}")
            return
        new = updater.whats_new(info)
        mb = f" ({info.size / 1e6:.0f} MB)" if info.size else ""
        text = (f"Version {info.version} is available (you have {APP_VERSION}).\n\n"
                + (f"What's new: {new}\n\n" if new else "")
                + f"Download and install it now{mb}? Your words and counts are kept, "
                  "and Word Counter restarts by itself.")
        box = QMessageBox(QMessageBox.Question, "Update Word Counter", text,
                          QMessageBox.Yes | QMessageBox.No, self)
        box.button(QMessageBox.Yes).setText("Update now")
        box.button(QMessageBox.No).setText("Later")
        if box.exec() == QMessageBox.Yes:
            self.start_download()

    def start_download(self):
        info = self.update_info
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Downloading… 0%")

        def work():
            try:
                path = updater.download(info, progress=lambda f: self.bridge.update_progress.emit(f))
                self.bridge.update_downloaded.emit(path)
            except Exception as e:
                self.bridge.update_downloaded.emit(e)
        threading.Thread(target=work, daemon=True).start()

    def on_update_progress(self, frac: float):
        self.update_btn.setText(f"Downloading… {int(frac * 100)}%")

    def on_update_downloaded(self, result):
        self.update_btn.setEnabled(True)
        if isinstance(result, Exception):
            self.update_btn.setText(f"⬆  Update to {self.update_info.version}")
            self.popup("Update failed", f"{result}\n\nYou can try again with the Update button.")
            return
        self.update_btn.setText("Installing…")
        try:
            updater.launch_installer(result)
        except Exception as e:
            self.popup("Update failed", f"Couldn't start the installer: {e}")
            return
        QTimer.singleShot(800, self.close)        # get out of the installer's way

    def popup(self, title: str, text: str):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(self, title, text)

    @staticmethod
    def _restyle(w):
        w.style().unpolish(w)
        w.style().polish(w)

    def closeEvent(self, e):
        self.stop_listening()
        if self.recognizer is not None:
            self.recognizer.stop()
        self.s.geometry = bytes(self.saveGeometry()).hex()
        self.s.save()
        super().closeEvent(e)
