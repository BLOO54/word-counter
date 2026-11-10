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

from . import APP_NAME, APP_VERSION, matching
from .audio import Listener
from .config import Settings, resource_dir
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
    ("pc", "🔊  PC sound (Discord, games, videos)"),
    ("both", "🎤+🔊  Both"),
]


class Bridge(QObject):
    """Lets the background threads talk to the window safely."""
    heard = Signal(str, str)
    ready = Signal(object)
    status = Signal(str, str)
    level = Signal(str, float)


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
        self.recognizer = None
        if start_engine:
            self.recognizer = Recognizer(self.bridge.heard.emit, self.bridge.ready.emit, self.hotwords)
            self.recognizer.start()
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

        heard = QLabel("WHAT I'M HEARING")
        heard.setObjectName("muted")
        lay.addWidget(heard)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(110)
        lay.addWidget(self.text, 2)

        bottom = QHBoxLayout()
        self.beep_box = QCheckBox("Beep when counted")
        self.beep_box.setChecked(self.s.beep)
        self.beep_box.toggled.connect(self.beep_changed)
        self.top_box = QCheckBox("Always on top")
        self.top_box.setChecked(self.s.always_on_top)
        self.top_box.toggled.connect(self.apply_on_top)
        reset = QPushButton("Reset all counts")
        reset.clicked.connect(self.reset_all)
        clear = QPushButton("Clear text")
        clear.clicked.connect(self.clear_text)
        bottom.addWidget(self.beep_box)
        bottom.addWidget(self.top_box)
        bottom.addStretch(1)
        bottom.addWidget(reset)
        bottom.addWidget(clear)
        lay.addLayout(bottom)

        ver = QLabel(f"v{APP_VERSION} · speech recognition runs on your PC, nothing is uploaded")
        ver.setObjectName("muted")
        ver.setStyleSheet("font-size: 11px;")
        lay.addWidget(ver)

        self.setCentralWidget(root)
        self.rebuild_cards()
        self.render_transcript()

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
        log.info("heard (%s): %s", source, text)
        self.transcript.append((source, text))
        self.transcript = self.transcript[-60:]
        hit = False
        for i, w in enumerate(self.s.words):
            n = matching.count(text, w["text"])
            if n:
                w["count"] += n
                hit = True
                if i < len(self.cards):
                    self.cards[i].count.setText(str(w["count"]))
                    self.cards[i].flash()
        if hit:
            self.s.save()
            if self.s.beep:
                self.beep()
        self.render_transcript()

    def render_transcript(self):
        phrases = [w["text"] for w in self.s.words]
        both = self.s.source == "both"
        out = []
        for source, text in self.transcript:
            pieces, last = [], 0
            for a, b in matching.highlight_spans(text, phrases):
                pieces.append(html.escape(text[last:a]))
                pieces.append(f'<span style="color:{PINK}; font-weight:700;">{html.escape(text[a:b])}</span>')
                last = b
            pieces.append(html.escape(text[last:]))
            tag = ("🎤 " if source == "mic" else "🔊 ") if both else ""
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
        return ["mic", "pc"] if self.s.source == "both" else [self.s.source]

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
        if not self.engine_ready:
            parts.append("Loading speech engine…")
        if self.listening:
            for kind in self.wanted_sources():
                parts.append(f"{Listener.NAMES[kind].capitalize()}: {self.source_status.get(kind, 'starting…')}")
        elif self.engine_ready:
            parts.append("Ready. Press Start Listening.")
        self.status.setText("   ·   ".join(parts))

    # ------------------------------------------------------------------ options
    def beep_changed(self, on):
        self.s.beep = on
        self.s.save()

    def apply_on_top(self, on, save=True):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        if save:
            self.s.always_on_top = bool(on)
            self.s.save()
            self.show()

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
