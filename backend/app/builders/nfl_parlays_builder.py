from __future__ import annotations

import json
from pathlib import Path

from app.utils.dates import timestamp_et


FANDUEL_SOURCE = "FanDuel Sportsbook"


def _load_fanduel_snapshot(path: Path) -> dict:
    if not path.exists():
        return {
            "source": FANDUEL_SOURCE,
            "date": None,
            "week": None,
            "season": None,
            "parlays": [],
            "source_status": "unavailable",
        }

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("source") != FANDUEL_SOURCE:
        raise ValueError("NFL parlay source must be FanDuel Sportsbook")

    for parlay in payload.get("parlays", []):
        if parlay.get("book") != FANDUEL_SOURCE:
            raise ValueError("Every NFL parlay must identify FanDuel Sportsbook")
        if not parlay.get("legs"):
            raise ValueError("FanDuel parlay cannot be empty")
        for leg in parlay["legs"]:
            if leg.get("book") != FANDUEL_SOURCE:
                raise ValueError("Every NFL leg must come from FanDuel Sportsbook")
            if not all(leg.get(field) for field in ("player_name", "market", "line", "selection")):
                raise ValueError("FanDuel leg is missing required market data")

    return payload


def build_nfl_parlays_board(*, config, paths) -> dict:
    del config
    source_path = paths.backend_root / "data_sources" / "fanduel_nfl_parlays.json"
    source = _load_fanduel_snapshot(source_path)
    parlays = []

    for item in source.get("parlays", []):
        odds_are_stable = item.get("odds_status") == "confirmed"
        parlays.append({
            "game_id": item["game_id"],
            "matchup": item["matchup"],
            "time": item.get("time", ""),
            "game_script": "fanduel market",
            "fanduel": {
                "legs": item["legs"],
                "odds": item.get("displayed_odds") if odds_are_stable else None,
                "odds_source": FANDUEL_SOURCE,
                "odds_status": item.get("odds_status", "unavailable"),
                "source_url": source.get("source_url"),
                "stake": None,
                "implied_win": None,
            },
        })

    return {
        "sport": "NFL",
        "date": source.get("date"),
        "last_updated": timestamp_et(),
        "captured_at": source.get("captured_at"),
        "week": source.get("week"),
        "season": source.get("season"),
        "source": FANDUEL_SOURCE,
        "source_status": source.get("source_status", "captured"),
        "uncertainty_note": "Only markets and lines displayed by FanDuel Sportsbook are eligible.",
        "parlays": parlays,
        "td_parlays": [],
    }
