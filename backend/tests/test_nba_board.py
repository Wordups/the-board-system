from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.main import run_nba_pipeline


def test_nba_pipeline_writes_json_outputs():
    board = run_nba_pipeline(PROJECT_ROOT)
    assert board["sport"] == "NBA"
    assert board["pinned_board"]["market"] == "PTS"
    assert board["hero_pick"] is None or board["hero_pick"]["player_name"]
    assert len(board["game_clusters"]) <= 3
    assert set(board["section_boards"].keys()) == {"PTS", "AST", "REB", "3PM", "LADDERS"}
    assert board["section_boards"]["PTS"]["title"] == "Scoring Board"
    assert board["games"]
    assert set(board["games"][0]["markets"].keys()) == {"PTS", "REB", "AST", "3PM", "ML"}
    assert (PROJECT_ROOT / "backend" / "data_final" / "nba.json").exists()
    assert (PROJECT_ROOT / "frontend" / "data" / "nba.json").exists()
    assert (PROJECT_ROOT / "data" / "nba.json").exists()
