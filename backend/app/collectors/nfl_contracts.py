"""NFL contract table — the salary axis of the historical salary board.

There is no free live feed for NFL contract money, so this reads a local file
rather than an API. Two locations, checked in order:

1. ``backend/data_raw/nfl_contracts.json`` — an untracked local/private
   override, for a fresh Spotrac / OverTheCap export you don't want committed.
2. ``backend/data_static/nfl_contracts.json`` — the tracked default that CI
   sees on a clean clone.

Every row carries its own provenance, and the board surfaces the file's
``meta`` block, so a consumer can always see how old the money is and whether
a figure was hand-entered. Nothing here is inferred: a player with no contract
row is simply absent from the salary board, never bucketed by guesswork.

Refresh the tracked file with ``backend/scripts/import_nfl_contracts.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.collectors.nfl_collector import player_key


# Salary categories, in descending order. Bands are league-wide money bands,
# not position-relative ranks: "what tier of money is this player paid", which
# is the question the historical board answers. Position context is applied on
# top of the tier, since $12M means something different for a guard than a QB.
SALARY_TIERS: tuple[tuple[str, float, float | None], ...] = (
    ("SUPERMAX", 40_000_000.0, None),
    ("ELITE", 25_000_000.0, 40_000_000.0),
    ("HIGH", 15_000_000.0, 25_000_000.0),
    ("MID", 7_000_000.0, 15_000_000.0),
    ("LOW", 2_500_000.0, 7_000_000.0),
    ("ROOKIE_MIN", 0.0, 2_500_000.0),
)

TIER_ORDER = tuple(name for name, _floor, _ceiling in SALARY_TIERS)

TIER_LABELS = {
    "SUPERMAX": "Supermax ($40M+ APY)",
    "ELITE": "Elite ($25M–$40M APY)",
    "HIGH": "High ($15M–$25M APY)",
    "MID": "Mid ($7M–$15M APY)",
    "LOW": "Low ($2.5M–$7M APY)",
    "ROOKIE_MIN": "Rookie / minimum (under $2.5M APY)",
}


def contracts_paths(paths) -> list[Path]:
    return [paths.data_raw / "nfl_contracts.json", paths.data_static / "nfl_contracts.json"]


def load_contracts(paths) -> dict[str, Any]:
    """Return ``{"meta": {...}, "players": {player_key: row}}``.

    A missing or malformed file is not an error — it degrades the salary board
    to ``available: false`` with a stated reason, exactly like an upstream
    outage does for the prediction board.
    """
    for path in contracts_paths(paths):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return empty_contracts(reason=f"{path.name} could not be read ({type(exc).__name__})")
        return parse_contracts(payload, source_path=path)
    return empty_contracts(reason="no contracts file found (see backend/data_static/nfl_contracts.example.json)")


def empty_contracts(*, reason: str) -> dict[str, Any]:
    return {"meta": {"loaded": False, "reason": reason}, "players": {}}


def parse_contracts(payload: dict[str, Any], *, source_path: Path) -> dict[str, Any]:
    meta = dict(payload.get("meta", {}))
    players: dict[str, dict[str, Any]] = {}

    for row in payload.get("players", []):
        name = str(row.get("player_name") or "").strip()
        apy = to_money(row.get("apy"))
        if not name or apy is None:
            continue  # a row without a name or a number can't be tiered
        key = player_key(name)
        players[key] = {
            "player_name": name,
            "player_key": key,
            "team": str(row.get("team") or "").upper(),
            "position": str(row.get("position") or "").upper(),
            "apy": apy,
            "total_value": to_money(row.get("total_value")),
            "guaranteed": to_money(row.get("guaranteed")),
            "years": row.get("years"),
            "signed": row.get("signed"),
            "salary_tier": salary_tier(apy),
            # True when the figure was hand-entered rather than imported from a
            # contract-database export. Surfaced on every board row.
            "estimated": bool(row.get("estimated", False)),
        }

    meta.update(
        {
            "loaded": True,
            "path": source_path.name,
            "player_count": len(players),
            "estimated_count": sum(1 for row in players.values() if row["estimated"]),
        }
    )
    return {"meta": meta, "players": players}


def salary_tier(apy: float) -> str:
    for name, floor, ceiling in SALARY_TIERS:
        if apy >= floor and (ceiling is None or apy < ceiling):
            return name
    return "ROOKIE_MIN"


def to_money(value: Any) -> float | None:
    """Accept 45000000, "45,000,000", "$45M", or "45.0" (millions is ambiguous,
    so a bare number under 1000 is read as millions)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number * 1_000_000 if number < 1000 else number
    text = str(value).strip().lower().replace("$", "").replace(",", "")
    if not text:
        return None
    multiplier = 1.0
    if text.endswith("m"):
        multiplier, text = 1_000_000.0, text[:-1]
    elif text.endswith("k"):
        multiplier, text = 1_000.0, text[:-1]
    try:
        number = float(text) * multiplier
    except ValueError:
        return None
    return number * 1_000_000 if number < 1000 else number
