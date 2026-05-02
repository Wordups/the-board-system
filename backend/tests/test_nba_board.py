from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.builders.nba_board_builder import build_market_diverse_top_signals
from app.main import run_nba_pipeline


def test_nba_pipeline_writes_json_outputs():
    board = run_nba_pipeline(PROJECT_ROOT)
    assert board["sport"] == "NBA"
    assert board["pinned_board"]["market"] == "PTS"
    assert board["consistency_board"]["market"] == "MIX"
    assert board["consistency_board"]["players"]
    assert board["hero_pick"] is None or board["hero_pick"]["player_name"]
    assert len(board["game_clusters"]) <= 3
    assert set(board["section_boards"].keys()) == {"PTS", "AST", "REB", "3PM", "LADDERS"}
    assert board["section_boards"]["PTS"]["title"] == "Scoring Board"
    assert board["games"]
    assert set(board["games"][0]["markets"].keys()) == {"PTS", "REB", "AST", "3PM", "ML"}
    assert (PROJECT_ROOT / "backend" / "data_final" / "nba.json").exists()
    assert (PROJECT_ROOT / "frontend" / "data" / "nba.json").exists()
    assert (PROJECT_ROOT / "data" / "nba.json").exists()


def test_nba_top_signals_diversify_markets_and_players():
    candidates = [
        {"market": "AST", "player_name": "A", "line": "6+ AST", "score": 80.0, "confidence": 80, "tier": "A"},
        {"market": "REB", "player_name": "B", "line": "9+ REB", "score": 79.0, "confidence": 79, "tier": "A"},
        {"market": "PTS", "player_name": "A", "line": "24+ PTS", "score": 78.0, "confidence": 78, "tier": "A"},
        {"market": "3PM", "player_name": "C", "line": "3+ 3PM", "score": 76.0, "confidence": 76, "tier": "B"},
    ]

    top_signals = build_market_diverse_top_signals(candidates=candidates, limit=3)

    assert [signal["market"] for signal in top_signals] == ["AST", "REB", "3PM"]
    assert len({signal["player_name"] for signal in top_signals}) == 3
