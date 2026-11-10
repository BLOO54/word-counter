"""The small in-game overlay: one line like  ● connor 12 · bro 34  in a corner of the screen.

Plain English: it's a see-through window that stays on top of games, can't be
clicked (clicks go straight through to the game) and never takes focus. It works
over games in "borderless" / "windowed fullscreen" mode. Games in exclusive
fullscreen draw over every other window, so nothing can show on top of those.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from . import win32

CORNERS = (("top-right", "Top right"), ("top-left", "Top left"),
           ("bottom-right", "Bottom right"), ("bottom-left", "Bottom left"))
SIZES = (("tiny", "Tiny", 10), ("small", "Small", 12), ("medium", "Medium", 15))
PINK = "#EC4899"
MAX_WORDS = 6
MARGIN = 12


class Overlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.WindowDoesNotAcceptFocus | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()                    # a child label paints its own rounded background
        self.label.setTextFormat(Qt.RichText)
        lay.addWidget(self.label)
        self.corner = "top-right"
        self.size_key = "small"
        self.words: list[dict] = []
        self.listening = False
        self.flash: set[str] = set()
        self.keep_on_top = QTimer(self)          # some games push themselves in front - push back
        self.keep_on_top.timeout.connect(self._on_top)
        self.keep_on_top.start(2000)

    def configure(self, corner: str, size_key: str):
        self.corner = corner if corner in dict(CORNERS) else "top-right"
        self.size_key = size_key if size_key in [s[0] for s in SIZES] else "small"
        self.refresh()

    def set_state(self, words: list[dict], listening: bool, flash: set[str] | None = None):
        self.words, self.listening = words, listening
        if flash:
            self.flash |= set(flash)
            QTimer.singleShot(700, lambda f=set(flash): self._unflash(f))
        self.refresh()

    def _unflash(self, words: set[str]):
        self.flash -= words
        self.refresh()

    def refresh(self):
        px = next(s[2] for s in SIZES if s[0] == self.size_key)
        dot = "#ff4d5e" if self.listening else "#6b7280"
        parts = []
        for w in self.words[:MAX_WORDS]:
            name = html.escape(w["text"])
            color = PINK if w["text"] in self.flash else "#e8eaf0"
            parts.append(f"<span style='color:{color}'>{name}</span> "
                         f"<b style='color:{color}'>{w['count']}</b>")
        more = f" <span style='color:#8b93a7'>+{len(self.words) - MAX_WORDS}</span>" \
            if len(self.words) > MAX_WORDS else ""
        text = (f"<span style='color:{dot}'>●</span>&nbsp; "
                + " <span style='color:#6b7280'>·</span> ".join(parts) + more) if parts else \
            f"<span style='color:{dot}'>●</span>&nbsp; <span style='color:#8b93a7'>no words yet</span>"
        pad_v, pad_h = max(3, px // 3), max(7, px * 2 // 3)
        self.label.setStyleSheet(f"QLabel {{ background: rgba(15, 17, 21, 175); color: #e8eaf0; "
                                 f"font-family: 'Segoe UI'; font-size: {px}px; font-weight: 600; "
                                 f"padding: {pad_v}px {pad_h}px; border-radius: {px}px; }}")
        self.label.setText(text)
        self.adjustSize()
        self._place()

    def _place(self):
        screen = QApplication.primaryScreen().geometry()       # full screen, so it sits over games too
        x = screen.left() + MARGIN if "left" in self.corner else screen.right() - self.width() - MARGIN
        y = screen.top() + MARGIN if "top" in self.corner else screen.bottom() - self.height() - MARGIN
        self.move(x, y)

    def showEvent(self, e):
        super().showEvent(e)
        win32.set_overlay_styles(int(self.winId()), click_through=True)

    def _on_top(self):
        if self.isVisible():
            hwnd = int(self.winId())
            win32.ensure_styles(hwnd, click_through=True)
            win32.force_topmost(hwnd)
