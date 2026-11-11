"""Milestone alerts (no Qt here, so it's easy to test)."""

from __future__ import annotations

MILESTONES = (0, 10, 25, 50, 100)          # 0 = off


def crossed_milestone(before: int, after: int, every: int) -> int | None:
    """The highest milestone passed going from `before` to `after` (e.g. 48 -> 51 with every=25 -> 50)."""
    if every <= 0 or after <= before:
        return None
    top = after // every * every
    return top if top > before and top > 0 else None


class LiveCounter:
    """Counting while someone is still talking, without counting anything twice.

    Each piece of speech is checked several times while it grows ("live"), then once more
    when it's finished ("final"). Live checks only ever ADD what's new since the last check;
    the final check is the most reliable, so it corrects the total for that piece either way.
    """

    def __init__(self):
        self.seen: dict[tuple, dict[str, int]] = {}
        self.done: list[tuple] = []          # recently finished pieces - ignore any late live check

    def update(self, source: str, piece: int, text: str, final: bool, words: list[str],
               similar: bool = True) -> dict[str, int]:
        from . import matching
        key = (source, piece)
        if key in self.done:
            return {}
        prev = self.seen.get(key, {})
        now = {w: matching.count(text, w, similar) for w in words}
        deltas = {}
        for w in words:
            d = now[w] - prev.get(w, 0)
            if not final:
                d = max(0, d)
            if d:
                deltas[w] = d
        if final:
            self.seen.pop(key, None)
            self.done = (self.done + [key])[-50:]
        else:
            self.seen[key] = {w: max(now[w], prev.get(w, 0)) for w in words}
        return deltas


def add_person(word: dict, person: str, delta: int) -> None:
    """Keep track of how many times each person (Discord bot mode) said this word."""
    by = word.setdefault("by", {})
    by[person] = max(0, by.get(person, 0) + delta)
    if not by[person]:
        del by[person]


def people_line(word: dict, names: dict, most: int = 3) -> tuple[str, str]:
    """('Connor 20 · Blade 10 · +2 more', full list for the tooltip) - most-said first."""
    by = word.get("by") or {}
    ranked = sorted(by.items(), key=lambda kv: -kv[1])
    parts = [f"{names.get(uid, 'someone')} {n}" for uid, n in ranked]
    if not parts:
        return "", ""
    short = " · ".join(parts[:most]) + (f" · +{len(parts) - most} more" if len(parts) > most else "")
    return short, "\n".join(parts)
