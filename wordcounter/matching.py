"""Finding your words inside what the speech recognition heard.

Plain English: we turn everything into lowercase words with no punctuation,
then look for your word (or phrase) as whole words only - so "bro" counts in
"bro, no way" but not in "brother".
"""

from __future__ import annotations

import re

_APOSTROPHES = re.compile(r"[’‘'`]")
_NOT_WORD = re.compile(r"[^0-9a-zÀ-ɏ]+")


def words_of(text: str) -> list[str]:
    """'Bro, that's CRAZY!' -> ['bro', 'thats', 'crazy']"""
    text = _APOSTROPHES.sub("", text.lower())
    return [w for w in _NOT_WORD.split(text) if w]


def count(text: str, phrase: str) -> int:
    """How many times `phrase` (one or more words) appears in `text` as whole words."""
    want = words_of(phrase)
    if not want:
        return 0
    have = words_of(text)
    n, i, size = 0, 0, len(want)
    while i + size <= len(have):
        if have[i:i + size] == want:
            n += 1
            i += size          # don't count overlapping matches twice
        else:
            i += 1
    return n


def highlight_spans(text: str, phrases: list[str]) -> list[tuple[int, int]]:
    """Character ranges in `text` where any of the phrases appears (for colouring the transcript)."""
    spans = []
    for phrase in phrases:
        parts = words_of(phrase)
        if not parts:
            continue
        gap = r"[^0-9A-Za-zÀ-ɏ]+"
        pattern = gap.join(
            "".join(re.escape(ch) + r"['’]?" for ch in p) for p in parts)
        rx = re.compile(r"(?<![0-9A-Za-zÀ-ɏ'’])" + pattern
                        + r"(?![0-9A-Za-zÀ-ɏ])", re.IGNORECASE)
        spans += [(m.start(), m.end()) for m in rx.finditer(text)]
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return merged
