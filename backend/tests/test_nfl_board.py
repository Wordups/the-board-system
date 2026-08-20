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

    # Same-PLAYER QB stat-stack correlation sim: a second, structurally
    # different extra field (own card shape, not a variant of
    # same_game_pairs above) -- see app/sim/nfl_qb_stack.py. Same
    # always-present, honestly-empty-when-ESPN-unreachable convention.
    assert "qb_stacks" in board
    assert isinstance(board["qb_stacks"], list)
    for stack in board["qb_stacks"]:
        assert set(stack.keys()) == {"game_id", "matchup", "qb", "floor", "ceiling"}
        assert set(stack["qb"].keys()) == {"player_name", "player_id"}
        for tier in ("floor", "ceiling"):
            rung = stack[tier]
            assert set(rung.keys()) == {"pass_yds_line", "completions_line", "pass_td_line", "joint_prob_pct"}
            assert rung["pass_yds_line"].endswith("+ Pass Yds")
            assert rung["completions_line"].endswith("+ Completions")
            assert rung["pass_td_line"].endswith("+ Pass TD")
            assert 0.0 < rung["joint_prob_pct"] <= 100.0

    # RB trend watch: two informational RB signals derived from 2025
    # game-by-game gamelogs (site.web.api.espn.com) -- a third extra field,
    # same always-present, honestly-empty-when-ESPN-unreachable convention
    # as same_game_pairs/qb_stacks above. See app/collectors/nfl_collector.py
    # build_rb_trend_watch.
    assert "rb_trend_watch" in board
    trend = board["rb_trend_watch"]
    assert set(trend.keys()) == {"window", "best_stretch", "trending_up"}
    assert isinstance(trend["best_stretch"], list)
    assert isinstance(trend["trending_up"], list)
    for row in trend["best_stretch"]:
        assert set(row.keys()) == {
            "player_id", "player_name", "team", "games_sampled",
            "best_stretch_total_yds", "best_stretch_avg_yds", "best_stretch_weeks", "season_avg_total_yds",
        }
    for row in trend["trending_up"]:
        assert set(row.keys()) == {
            "player_id", "player_name", "team", "games_sampled",
            "season_avg_total_yds", "recent_avg_total_yds", "trend_pct", "recent_weeks",
        }
        assert row["trend_pct"] > 0

    if board["games"]:
        game = board["games"][0]
        # Superset, not equality: which markets actually populate for a given
        # game is data-dependent (a market needs real qualifying stats to
        # appear), and a stale last-good fallback (this test's own network
        # outage path, see above) can legitimately predate a newly added
        # market like PassYds.
        assert set(game["markets"].keys()) <= {"TD", "RecYds", "RushYds", "REC", "PassTD", "PassYds", "Completions", "INT", "ML"}
        for market_rows in game["markets"].values():
            for row in market_rows:
                assert row["lineup_confirmed"] is False
                assert 1 <= row["confidence"] <= 99
                assert row["tier"] in {"A", "B", "C", "PASS"}
