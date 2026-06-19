from __future__ import annotations


def to_confidence(score: float) -> int:
    """Confidence on the same 0-100 scale as the score.

    The score is now the model's simulated clear probability (0-100), so the
    confidence simply tracks it. Clamped to [0, 100] — the old [1, 99] floor/
    ceiling was an artifact of the previous ~0-50 score range and would fight
    legitimate near-0 / near-100 probabilities.
    """
    return max(0, min(100, round(score)))
