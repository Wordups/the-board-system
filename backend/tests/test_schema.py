from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.schemas.board_schema import BoardPayload


def test_board_schema_accepts_contract_shape():
    payload = {
        "sport": "MLB",
        "date": "2026-04-28",
        "last_updated": "10:45 AM ET",
        "pinned_board": {
            "title": "HR Top 10",
            "market": "HR",
            "players": [],
        },
        "consistency_board": {
            "title": "Consistency Top 10",
            "market": "MIX",
            "players": [],
        },
        "games": [
            {
                "game_id": "nyy-bos-2026-04-28",
                "matchup": "NYY @ BOS",
                "time": "7:05 PM ET",
                "top_signals": [],
                "markets": {"HR": [], "K": [], "Hits": [], "TB": [], "ML": []},
            }
        ],
    }
    parsed = BoardPayload.model_validate(payload)
    assert parsed.sport == "MLB"


def test_board_schema_accepts_nba_market_shape():
    payload = {
        "sport": "NBA",
        "date": "2026-04-28",
        "last_updated": "8:15 PM ET",
        "pinned_board": {
            "title": "PTS Top 10",
            "market": "PTS",
            "players": [],
        },
        "consistency_board": None,
        "games": [
            {
                "game_id": "0042500115",
                "matchup": "PHI @ BOS",
                "time": "Q1 2:59",
                "top_signals": [],
                "markets": {"PTS": [], "REB": [], "AST": [], "3PM": [], "ML": []},
            }
        ],
    }
    parsed = BoardPayload.model_validate(payload)
    assert parsed.sport == "NBA"
