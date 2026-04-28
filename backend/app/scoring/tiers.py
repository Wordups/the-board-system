from __future__ import annotations


def assign_tier(score: float) -> str:
    if score >= 28:
        return "A"
    if score >= 22:
        return "B"
    if score >= 16:
        return "C"
    return "PASS"
