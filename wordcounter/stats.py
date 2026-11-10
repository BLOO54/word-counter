"""Milestone alerts (no Qt here, so it's easy to test)."""

from __future__ import annotations

MILESTONES = (0, 10, 25, 50, 100)          # 0 = off


def crossed_milestone(before: int, after: int, every: int) -> int | None:
    """The highest milestone passed going from `before` to `after` (e.g. 48 -> 51 with every=25 -> 50)."""
    if every <= 0 or after <= before:
        return None
    top = after // every * every
    return top if top > before and top > 0 else None
