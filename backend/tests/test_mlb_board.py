from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.builders.mlb_board_builder import apply_hr_board_sliding_scale
from app.collectors.mlb_collector import build_player_hr_results
from app.main import run_mlb_pipeline
from app.models.mlb_model import MlbPlayCandidate
from app.scoring.edge_score import score_candidate


def test_mlb_pipeline_writes_json_outputs():
    board = run_mlb_pipeline(PROJECT_ROOT)
    assert board["sport"] == "MLB"
    assert board["pinned_board"]["players"]
    assert board["games"][0]["top_signals"]
    assert (PROJECT_ROOT / "backend" / "data_processed" / "mlb_processed.json").exists()
    assert (PROJECT_ROOT / "backend" / "data_final" / "mlb.json").exists()
    assert (PROJECT_ROOT / "frontend" / "data" / "mlb.json").exists()
    assert (PROJECT_ROOT / "data" / "mlb.json").exists()


def test_hr_board_sliding_scale_decays_live_games():
    pregame = apply_hr_board_sliding_scale(
        base_score=30.0,
        previous_score=28.0,
        status={"phase": "pregame", "minutes_to_start": 45, "is_lineup_window": True, "probable_pitchers_confirmed": True},
    )
    live = apply_hr_board_sliding_scale(
        base_score=30.0,
        previous_score=28.0,
        status={"phase": "live", "current_inning": 6},
    )
    final = apply_hr_board_sliding_scale(
        base_score=30.0,
        previous_score=28.0,
        status={"phase": "final"},
    )
    late_live = apply_hr_board_sliding_scale(
        base_score=30.0,
        previous_score=28.0,
        status={"phase": "live", "current_inning": 8},
    )
    assert pregame > live
    assert live > late_live
    assert final == 0.0


def test_build_player_hr_results_marks_hit_and_miss():
    game_boxscore = {
        "teams": {
            "away": {
                "players": {
                    "ID1": {
                        "person": {"id": 1},
                        "stats": {"batting": {"homeRuns": 1}},
                    }
                }
            },
            "home": {
                "players": {
                    "ID2": {
                        "person": {"id": 2},
                        "stats": {"batting": {"homeRuns": 0}},
                    }
                }
            },
        }
    }

    results = build_player_hr_results(game_boxscore, {"phase": "final"})

    assert results["1"]["result"] == "hit"
    assert results["1"]["home_runs"] == 1
    assert results["2"]["result"] == "miss"


def test_mlb_pipeline_hides_final_games_from_active_slate():
    board = run_mlb_pipeline(PROJECT_ROOT)
    assert all(game["top_signals"] for game in board["games"])


def test_hr_reason_surfaces_power_factors():
    candidate = MlbPlayCandidate(
        player_id="1",
        player_name="Slugger",
        team="NYY",
        opponent="BOS",
        game_id="nyy-bos-2026-04-29",
        market="HR",
        line="HR 1+",
        stat_value=0.42,
        baseline=0.08,
        trend=0.55,
        matchup=0.61,
        recent_form=0.72,
        extra={
            "season_hr_per_game": 0.28,
            "l10_hr_per_game": 0.40,
            "l5_hr_per_game": 0.60,
            "ops": 0.945,
            "slg": 0.612,
            "lineup_spot": 2,
            "pitcher_matchup": 0.64,
        },
    )

    scored = score_candidate(candidate)

    assert "L5 0.60/g" in scored.reason
    assert "OPS 0.945" in scored.reason
    assert "Batting 2" in scored.reason
