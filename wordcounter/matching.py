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


def sound_key(word: str) -> str:
    """Soundex: words that sound alike get the same key ('connor', 'conner', 'conor' -> C560)."""
    codes = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"), **dict.fromkeys("dt", "3"),
             "l": "4", **dict.fromkeys("mn", "5"), "r": "6"}
    word = "".join(ch for ch in word.lower() if ch.isalpha())
    if not word:
        return ""
    out, last = word[0].upper(), codes.get(word[0], "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != last:
            out += code
        if ch not in "hw":
            last = code
    return (out + "000")[:4]


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def same_word(heard: str, wanted: str, similar: bool = True) -> bool:
    """Exact match - or, with `similar`:
    - the word with 's on the end ("connor's" / "connors" counts for "connor", "bros" for "bro")
    - for words of 4+ letters, a different spelling that sounds the same ("conner", "conor")."""
    if heard == wanted:
        return True
    if not similar or heard.isdigit() or wanted.isdigit():
        return False
    if heard == wanted + "s":
        return True
    if len(wanted) < 4:
        return False
    base = heard[:-1] if heard.endswith("s") and not wanted.endswith("s") else heard
    limit = 1 if len(wanted) < 6 else 2
    return sound_key(base) == sound_key(wanted) and edit_distance(base, wanted) <= limit


def count(text: str, phrase: str, similar: bool = True) -> int:
    """How many times `phrase` (one or more words) appears in `text` as whole words.
    With `similar`, other spellings that sound the same also count (connor / conner / conor)."""
    want = words_of(phrase)
    if not want:
        return 0
    have = words_of(text)
    n, i, size = 0, 0, len(want)
    while i + size <= len(have):
        if all(same_word(h, w, similar) for h, w in zip(have[i:i + size], want)):
            n += 1
            i += size          # don't count overlapping matches twice
        else:
            i += 1
    return n


_TOKEN = re.compile(r"[0-9A-Za-z\u00c0-\u024f]+(?:['\u2019`][0-9A-Za-z\u00c0-\u024f]+)*")


def highlight_spans(text: str, phrases: list[str], similar: bool = True) -> list[tuple[int, int]]:
    """Character ranges in `text` where any of the phrases appears (for colouring the transcript)."""
    tokens = [(m.start(), m.end(), "".join(words_of(m.group()))) for m in _TOKEN.finditer(text)]
    spans = []
    for phrase in phrases:
        want = words_of(phrase)
        if not want:
            continue
        size, i = len(want), 0
        while i + size <= len(tokens):
            window = tokens[i:i + size]
            if all(same_word(t[2], w, similar) for t, w in zip(window, want)):
                spans.append((window[0][0], window[-1][1]))
                i += size
            else:
                i += 1
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s_, e in spans:
        if merged and s_ <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s_, e))
    return merged
