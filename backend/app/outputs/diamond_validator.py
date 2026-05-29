"""Diamond field validation (from the generated diamond package).

Adapted to the-board-system: this repo has no book odds, so `edge` is 0.0 and
`american` is null — both still validated for type/range. Called after the
Pydantic BoardPayload check in outputs/validator.py.
"""
from __future__ import annotations

from typing import Any

VALID_POSITIONS = {"1B", "2B", "3B", "HOME", "MOUND"}
REQUIRED_TOP = ("date", "early_or_late", "picks", "hr_count", "hr_floor", "is_valid")
REQUIRED_PICK = ("name", "team", "market", "prob", "edge", "american", "reasoning")


def validate_diamond_field(board_payload: dict[str, Any]) -> bool:
    diamond = board_payload.get("diamond")
    if diamond is None:
        return True  # optional

    missing = [f for f in REQUIRED_TOP if f not in diamond]
    if missing:
        raise ValueError(f"Diamond missing top-level fields: {missing}")
    if diamond["early_or_late"] not in ("EARLY", "LATE"):
        raise ValueError(f"Invalid early_or_late: {diamond['early_or_late']}")

    picks = diamond["picks"]
    if not isinstance(picks, dict):
        raise ValueError("Diamond picks must be a dict")

    for pos, pick in picks.items():
        if pos not in VALID_POSITIONS:
            raise ValueError(f"Invalid Diamond position: {pos}")
        pick_missing = [f for f in REQUIRED_PICK if f not in pick]
        if pick_missing:
            raise ValueError(f"Diamond pick {pos} missing fields: {pick_missing}")
        if not isinstance(pick["prob"], (int, float)) or not (0 <= pick["prob"] <= 1):
            raise ValueError(f"Diamond pick {pos} prob out of range: {pick['prob']}")
        if not isinstance(pick["edge"], (int, float)) or not (-1 <= pick["edge"] <= 1):
            raise ValueError(f"Diamond pick {pos} edge out of range: {pick['edge']}")
        if pick["american"] is not None and not isinstance(pick["american"], int):
            raise ValueError(f"Diamond pick {pos} american must be int or null")

    if diamond["hr_count"] < 0 or diamond["hr_floor"] < 0:
        raise ValueError("Diamond hr_count and hr_floor must be >= 0")
    return True
