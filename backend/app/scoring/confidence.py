from __future__ import annotations


def to_confidence(score: float) -> int:
    return max(1, min(99, round(score)))
