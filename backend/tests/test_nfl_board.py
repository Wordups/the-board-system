from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.main import run_nfl_pipeline
from app.outputs.validator import validate_board_payload


def test_nfl_pipeline_writes_json_outputs():
    # Runs the real pipeline against live ESPN endpoints, same convention as
    # test_soccer_board.py / test_wnba_board.py. The collector degrades to an
    # empty-but-schema-valid board (games: []) rather than raising if ESPN is
    # unreachable from the test runner, so this passes either way — but when
    # ESPN is reachable it also exercises the full collector/model/builder
    # path against real Week 1 2026 data.
    board = run_nfl_pipeline(PROJECT_ROOT)
    assert board["sport"] == "NFL"
    assert board["pinned_board"]["market"] == "TD"
    assert set(board.keys()) >= {"sport", "date", "last_updated", "pinned_board", "games"}
    validate_board_payload(board)
    assert (PROJECT_ROOT / "backend" / "data_final" / "nfl.json").exists()
    assert (PROJECT_ROOT / "frontend" / "data" / "nfl.json").exists()
    assert (PROJECT_ROOT / "data" / "nfl.json").exists()

    # Same-game QB<->receiver correlation sim: extra field the generic board
    # schema/pipeline ignores (same pattern as `diamond` for MLB). Must
    # always be present and list-shaped -- honestly empty ([]) when ESPN is
    # unreachable in this sandbox rather than absent or fabricated.
    assert "same_game_pairs" in board
    assert isinstance(board["same_game_pairs"], list)
    for pair in board["same_game_pairs"]:
        assert set(pair.keys()) == {"game_id", "matchup", "qb", "receiver", "floor", "ceiling"}
        assert set(pair["qb"].keys()) == {"player_name", "player_id"}
        assert set(pair["receiver"].keys()) == {"player_name", "player_id"}
        for tier in ("floor", "ceiling"):
            rung = pair[tier]
            assert set(rung.keys()) == {"qb_line", "receiver_line", "joint_prob_pct"}
            assert rung["qb_line"].endswith("+ Pass TD")
            assert rung["receiver_line"].endswith("+ Rec TD")
            assert 0.0 < rung["joint_prob_pct"] <= 100.0

    if board["games"]:
        game = board["games"][0]
        assert set(game["markets"].keys()) == {"TD", "RecYds", "RushYds", "REC", "PassTD", "ML"}
        for market_rows in game["markets"].values():
            for row in market_rows:
                assert row["lineup_confirmed"] is False
                assert 1 <= row["confidence"] <= 99
                assert row["tier"] in {"A", "B", "C", "PASS"}
