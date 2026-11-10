"""The Word Counter window."""

from __future__ import annotations

import html
import logging
import sys
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout,
    QWidget,
)

from . import APP_NAME, APP_VERSION, gpu, matching, models, stats, updater, vram
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
    heard = Signal(str, str, int, bool)
    ready = Signal(object)
    status = Signal(str, str)
    level = Signal(str, float)
    update_result = Signal(object, bool)
    update_progress = Signal(float)
    update_downloaded = Signal(object)
    model_progress = Signal(float)
    model_done = Signal(str, object)
    switch_failed = Signal(str, str)
    gpu_progress = Signal(float)
    gpu_done = Signal(object)
    measured = Signal(str, object)


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


class Toast(QWidget):
    """A small pop-up in the bottom-right corner of the screen. It never takes focus and clicks
    go straight through it, so it can't interrupt a game."""

    def __init__(self, text: str):
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)       # lets the corners be rounded
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(text)                             # a child label paints its background
        self.label.setStyleSheet(f"QLabel {{ background: {GRADIENT}; color: white; font-family: 'Segoe UI'; "
                                 "font-size: 20px; font-weight: 700; padding: 14px 22px; "
                                 "border-radius: 14px; }")
        lay.addWidget(self.label)
        self.adjustSize()

    def show_for(self, ms: int):
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)
        self.show()
        QTimer.singleShot(ms, self.close)


class DownloadsDialog(QDialog):
    """Every speech model with its download size and graphics memory (VRAM), plus Remove buttons."""

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Models & downloads")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(620)
        self.lay = QVBoxLayout(self)
        self.build()

    def build(self):
        _clear_layout(self.lay)
        w = self.win
        title = QLabel("Models & downloads")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        self.lay.addWidget(title)

        card = vram.card_memory()
        if card:
            used, total = card
            mine = w.s.vram_measured.get(w.s.accuracy) if (w.recognizer and w.recognizer.on_gpu) else None
            line = f"Your graphics card: {vram.gb(used)} of {vram.gb(total)} in use"
            if mine:
                line += f"  (Word Counter ≈ {vram.gb(mine)})"
        else:
            line = "Graphics card memory can't be read on this PC - the numbers below are typical values."
        top = QLabel(line)
        top.setObjectName("muted")
        top.setWordWrap(True)
        self.lay.addWidget(top)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        for col, head in enumerate(("Model", "Download", "Graphics memory", "Status", "")):
            h = QLabel(head)
            h.setObjectName("muted")
            h.setStyleSheet("font-size: 12px; font-weight: 600;")
            grid.addWidget(h, 0, col)
        total = 0
        row = 1
        for key in models.ORDER:
            repo, label, size, about = models.MODELS[key]
            installed = w.store.installed(key)
            if repo is None:
                dl = "built in"
            elif installed:
                n = models.folder_size(w.store.path(key))
                total += n
                dl = vram.gb(n) if n >= 1e9 else f"{n / 1e6:.0f} MB"
            else:
                dl = size
            in_use = w.s.accuracy == key
            status = ("in use" + (" (graphics card)" if w.recognizer and w.recognizer.on_gpu else " (processor)")
                      if in_use else "downloaded" if installed else "not downloaded")
            name = QLabel(label)
            name.setStyleSheet("font-weight: 600;" + (f" color: {PINK};" if in_use else ""))
            name.setToolTip(about)
            grid.addWidget(name, row, 0)
            grid.addWidget(QLabel(dl), row, 1)
            grid.addWidget(QLabel(vram.model_vram_text(key, w.s.vram_measured, w.s.careful)), row, 2)
            st = QLabel(status)
            st.setObjectName("muted")
            grid.addWidget(st, row, 3)
            if repo is not None and installed:
                b = QPushButton("Remove")
                b.setObjectName("small")
                b.clicked.connect(lambda _=False, k=key: self.remove_model(k))
                grid.addWidget(b, row, 4)
            row += 1
        if gpu.installed():
            n = models.folder_size(gpu.libs_dir())
            total += n
            grid.addWidget(QLabel("Graphics card support"), row, 0)
            grid.addWidget(QLabel(vram.gb(n)), row, 1)
            grid.addWidget(QLabel("-"), row, 2)
            st = QLabel("in use" if w.s.use_gpu else "downloaded")
            st.setObjectName("muted")
            grid.addWidget(st, row, 3)
            b = QPushButton("Remove")
            b.setObjectName("small")
            b.clicked.connect(self.remove_gpu)
            grid.addWidget(b, row, 4)
        self.lay.addLayout(grid)

        note = QLabel(f"Downloads take {vram.gb(total)} on your PC. Graphics memory is only used while a model "
                      "runs on the graphics card - on the processor it uses normal memory instead. "
                      "Careful mode adds a little. Removed models can be downloaded again any time.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        self.lay.addWidget(note)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        self.lay.addWidget(close, 0, Qt.AlignRight)

    def remove_model(self, key):
        w = self.win
        if w.s.accuracy == key:
            w.use_model("fast")          # stop using it first
        QTimer.singleShot(1500 if w.s.accuracy == "fast" else 0, lambda: (w.store.remove(key), self.build()))

    def remove_gpu(self):
        w = self.win
        if w.s.use_gpu:
            w.gpu_box.setChecked(False)  # move back to the processor first
        # the files are still loaded by Windows until Word Counter restarts, so remove them on next start
        try:
            (gpu.libs_dir() / "remove-me").write_text("1", encoding="utf-8")
        except OSError:
            pass
        w.popup("Graphics card support", "It will be removed the next time Word Counter starts.")
        self.accept()


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear_layout(item.layout())


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, start_engine: bool = True):
        super().__init__()
        self.s = settings
        self.listeners: dict[str, Listener] = {}
        self.listening = False
        self.engine_ready = False
        self.engine_error: str | None = None
        self.transcript: list[dict] = []          # {key, source, text, final}
        self.live_counter = stats.LiveCounter()
        self.source_status: dict[str, str] = {}
        self.cards: list[WordCard] = []
        self._lock = threading.Lock()
        self.update_info = None
        self._update_busy = False
        self.store = models.ModelStore()
        self.model_note = ""
        self.recognizer = None
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
        self.bridge.gpu_progress.connect(self.on_gpu_progress)
        self.bridge.gpu_done.connect(self.on_gpu_done)
        self.bridge.measured.connect(self.on_measured)
        if start_engine:
            acc = self.s.accuracy
            path = self.store.path(acc) if acc != "fast" and self.store.installed(acc) else None
            if path is None:
                self.s.accuracy = "fast"
                self.set_accuracy_box("fast")
            use_gpu = self.s.use_gpu and gpu.installed() and gpu.activate()
            if not use_gpu and self.s.use_gpu:
                self.s.use_gpu = False
                self.gpu_box.setChecked(False)
            self.recognizer = Recognizer(self.bridge.heard.emit, self.bridge.ready.emit, self.hotwords,
                                         model_path=path, use_gpu=bool(use_gpu),
                                         on_switch_failed=self.bridge.switch_failed.emit)
            self.recognizer.beam = 5 if self.s.careful else 1
            self.recognizer.on_measured = self.bridge.measured.emit
            QTimer.singleShot(60_000, self.clean_old_installers)
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
        self.gpu_box = QCheckBox("Use my graphics card (NVIDIA) - makes Better/Best fast")
        self.gpu_box.setChecked(self.s.use_gpu)
        if not gpu.has_nvidia_driver():
            self.gpu_box.setEnabled(False)
            self.gpu_box.setToolTip("No NVIDIA graphics driver found on this PC")
        self.gpu_box.toggled.connect(self.gpu_changed)
        self.careful_box = QCheckBox("Careful mode (more accurate, a bit slower)")
        self.careful_box.setChecked(self.s.careful)
        self.careful_box.toggled.connect(self.careful_changed)
        self.live_box = QCheckBox("Count while they're talking (faster)")
        self.live_box.setChecked(self.s.live)
        self.live_box.setToolTip("Checks each sentence while it's still being said. Best with the graphics card on.")
        self.live_box.toggled.connect(self.live_changed)
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
        options.addWidget(self.gpu_box, 2, 0, 1, 2)
        ms_row = QHBoxLayout()
        ms_lbl = QLabel("Milestone alert:")
        ms_lbl.setObjectName("muted")
        self.milestone_box = QComboBox()
        for n in stats.MILESTONES:
            self.milestone_box.addItem("Off" if n == 0 else f"every {n}", n)
        self.milestone_box.setCurrentIndex(list(stats.MILESTONES).index(self.s.milestone))
        self.milestone_box.currentIndexChanged.connect(self.milestone_changed)
        ms_row.addWidget(ms_lbl)
        ms_row.addWidget(self.milestone_box)
        ms_row.addStretch(1)
        options.addWidget(self.careful_box, 3, 0)
        options.addWidget(self.live_box, 3, 1)
        downloads = QPushButton("Models & downloads…")
        downloads.setObjectName("small")
        downloads.setToolTip("See and remove downloaded accuracy models and graphics card files")
        downloads.clicked.connect(self.show_downloads)
        ms_row.addSpacing(16)
        ms_row.addWidget(downloads)
        options.addLayout(ms_row, 4, 0, 1, 2)
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
    def on_heard(self, source: str, text: str, piece: int = -1, final: bool = True):
        key = (source, piece)
        if self.s.show_text:
            entry = next((e for e in self.transcript if e["key"] == key), None)
            if final and not text:
                if entry:
                    self.transcript.remove(entry)
            elif entry:
                entry["text"], entry["final"] = text, final
            else:
                self.transcript.append({"key": key, "source": source, "text": text, "final": final})
                self.transcript = self.transcript[-60:]
        deltas = self.live_counter.update(source, piece, text, final,
                                          [w["text"] for w in self.s.words], self.s.similar)
        hit = changed = False
        milestones = []
        for i, w in enumerate(self.s.words):
            d = deltas.get(w["text"], 0)
            if not d:
                continue
            before = w["count"]
            w["count"] = max(0, before + d)
            changed = True
            if i < len(self.cards):
                self.cards[i].count.setText(str(w["count"]))
            if w["count"] > before:
                hit = True
                if i < len(self.cards):
                    self.cards[i].flash()
                reached = stats.crossed_milestone(before, w["count"], self.s.milestone)
                if reached:
                    milestones.append((w["text"], reached))
        if changed:
            log.info("counted from %s%s: %s", source, "" if final else " (live)",
                     ", ".join(f"{w['text']}={w['count']}" for w in self.s.words))
            self.s.save()
        if hit:
            if milestones:
                self.celebrate(milestones)
            elif self.s.beep:
                self.beep()
        if self.s.show_text:
            self.render_transcript()

    def celebrate(self, reached: list[tuple[str, int]]):
        """Milestone: a rising 3-note sound and a pop-up that shows even over a game."""
        text = "   ".join(f"🎉 “{w}” hit {n}!" for w, n in reached)
        if sys.platform == "win32":
            def go():
                try:
                    import winsound
                    for f in (660, 880, 1175):
                        winsound.Beep(f, 120)
                except Exception:
                    pass
            threading.Thread(target=go, daemon=True).start()
        else:
            QApplication.beep()
        self.toast = Toast(text)
        self.toast.show_for(3500)

    def render_transcript(self):
        phrases = [w["text"] for w in self.s.words]
        several = len(SOURCE_PARTS[self.s.source]) > 1
        out = []
        for e in self.transcript:
            source, text = e["source"], e["text"]
            pieces, last = [], 0
            for a, b in matching.highlight_spans(text, phrases, self.s.similar):
                pieces.append(html.escape(text[last:a]))
                pieces.append(f'<span style="color:{PINK}; font-weight:700;">{html.escape(text[a:b])}</span>')
                last = b
            pieces.append(html.escape(text[last:]))
            tag = SOURCE_TAGS.get(source, "") if several else ""
            style = "margin-bottom:4px" + ("" if e["final"] else "; color:#7d8496; font-style:italic")
            out.append(f"<div style='{style}'>{tag}{''.join(pieces)}</div>")
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
            lst = Listener(kind, self._piece, self.bridge.status.emit, self.bridge.level.emit,
                           live=lambda: self.s.live)
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

    def _piece(self, source, audio, piece=-1, final=True):
        if self.recognizer is not None:
            self.recognizer.submit(source, audio, piece, final)

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
        if self.engine_ready and not self.model_note and self.recognizer is not None:
            parts.append("running on the graphics card ⚡" if self.recognizer.on_gpu else "running on the processor")
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

    def on_measured(self, key, n_bytes):
        if key in models.MODELS:
            self.s.vram_measured[key] = int(n_bytes)
            self.s.save()
            self.fill_accuracy_box()

    def careful_changed(self, on):
        self.s.careful = bool(on)
        self.s.save()
        if self.recognizer is not None:
            self.recognizer.beam = 5 if on else 1
        self.fill_accuracy_box()

    def live_changed(self, on):
        self.s.live = bool(on)        # listeners ask for this each time, so it applies straight away
        self.s.save()

    def show_downloads(self):
        DownloadsDialog(self).exec()
        self.fill_accuracy_box()

    def milestone_changed(self, _i):
        self.s.milestone = int(self.milestone_box.currentData())
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
        icons = {"fast": "⚡", "better": "🎯", "best": "🏆", "max": "💎"}
        for key in models.ORDER:
            repo, label, size, about = models.MODELS[key]
            extra = "" if repo is None or self.store.installed(key) else f"  ({size} download)"
            mem = vram.model_vram_text(key, self.s.vram_measured, self.s.careful).replace(" (measured)", "")
            self.accuracy.addItem(f"{icons[key]}  {label}{extra}  ·  {mem} VRAM", key)
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
                          f"It {about}. It runs on your PC like the built-in one."
                          + ("\n\nWithout “Use my graphics card” switched on it will be very slow."
                             if key == "max" and not self.s.use_gpu else "")
                          + "\n\nDownload it now?",
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
            self.recognizer.switch(self.store.path(key) if key != "fast" else None, self.gpu_wanted())

    def gpu_wanted(self) -> bool:
        return bool(self.s.use_gpu and gpu.installed() and gpu.activate())

    def on_switch_failed(self, msg, what):
        if what == "gpu":
            self.s.use_gpu = False
            self.gpu_box.blockSignals(True)
            self.gpu_box.setChecked(False)
            self.gpu_box.blockSignals(False)
        else:
            self.s.accuracy = getattr(self, "_prev_accuracy", "fast") if self.engine_ready else "fast"
            self.set_accuracy_box(self.s.accuracy)
        self.s.save()
        self.model_note = ""
        self.update_status()
        self.popup("Heads up", msg)

    def gpu_changed(self, on):
        from PySide6.QtWidgets import QMessageBox
        on = bool(on)
        if self.downloading:
            self.gpu_box.blockSignals(True)
            self.gpu_box.setChecked(self.s.use_gpu)
            self.gpu_box.blockSignals(False)
            return
        if not on or gpu.installed():
            self.s.use_gpu = on
            self.s.save()
            if on and not gpu.activate():
                return
            self.engine_ready = False
            self.model_note = "Moving speech recognition to the " + ("graphics card…" if on else "processor…")
            self.update_status()
            if self.recognizer is not None:
                acc = self.s.accuracy
                self.recognizer.switch(self.store.path(acc) if acc != "fast" else None, on)
            return
        box = QMessageBox(QMessageBox.Question, "Use your graphics card?",
                          "To run on your NVIDIA graphics card, Word Counter needs NVIDIA's speech-maths "
                          "libraries (cuBLAS and cuDNN) - a one-time download of about 1 GB, straight from "
                          "NVIDIA's official packages.\n\nAfterwards Word Counter restarts by itself.\n\n"
                          "Download now?", QMessageBox.Yes | QMessageBox.No, self)
        box.button(QMessageBox.Yes).setText("Download")
        box.button(QMessageBox.No).setText("Cancel")
        if box.exec() != QMessageBox.Yes:
            self.gpu_box.blockSignals(True)
            self.gpu_box.setChecked(False)
            self.gpu_box.blockSignals(False)
            return
        self.downloading = "gpu"
        self.gpu_box.setEnabled(False)
        self.accuracy.setEnabled(False)

        def work():
            try:
                gpu.download(progress=lambda d, t: self.bridge.gpu_progress.emit(d / t if t else 0))
                self.bridge.gpu_done.emit(None)
            except Exception as e:
                log.exception("graphics card download failed")
                self.bridge.gpu_done.emit(e)
        threading.Thread(target=work, daemon=True).start()

    def on_gpu_progress(self, frac):
        self.model_note = f"Downloading graphics card support… {int(frac * 100)}%"
        self.update_status()

    def on_gpu_done(self, error):
        self.downloading = None
        self.model_note = ""
        self.gpu_box.setEnabled(True)
        self.accuracy.setEnabled(True)
        if error is not None:
            self.gpu_box.blockSignals(True)
            self.gpu_box.setChecked(False)
            self.gpu_box.blockSignals(False)
            self.update_status()
            self.popup("Download failed", f"{error}\n\nCheck your internet and try again.")
            return
        self.s.use_gpu = True
        self.s.save()
        self.popup("Graphics card ready", "Downloaded! Word Counter will now restart to use your graphics card.")
        self.restart()

    def restart(self):
        from PySide6.QtCore import QProcess
        if getattr(sys, "frozen", False):
            QProcess.startDetached(sys.executable, [])
        else:
            QProcess.startDetached(sys.executable, [str(resource_dir() / "main.py")])
        self.close()

    def clean_old_installers(self):
        """Delete installers left behind by earlier updates (about 250 MB each)."""
        import tempfile
        from pathlib import Path
        folder = Path(tempfile.gettempdir()) / "WordCounter-update"
        freed = 0
        for f in folder.glob("*"):
            try:
                size = f.stat().st_size
                f.unlink()
                freed += size
            except OSError:
                pass                     # still in use - next start will get it
        if freed:
            log.info("removed old update installers (%.0f MB)", freed / 1e6)

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
