from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.builders.mlb_board_builder import apply_hr_board_sliding_scale, build_market_diverse_top_signals
from app.collectors.mlb_collector import build_hitter_inputs, build_player_hr_results
from app.main import run_mlb_pipeline
from app.models.mlb_model import MlbPlayCandidate
from app.scoring.edge_score import score_candidate


def test_mlb_pipeline_writes_json_outputs():
    board = run_mlb_pipeline(PROJECT_ROOT)
    assert board["sport"] == "MLB"
    assert board["pinned_board"]["players"]
    assert board["consistency_board"]["players"]
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


def test_market_diverse_top_signals_prioritize_consistency_markets():
    candidates = [
        MlbPlayCandidate(player_id="1", player_name="Hit Guy", team="NYY", opponent="BOS", game_id="g1", market="Hits", line="1+ Hit", stat_value=0.8, baseline=0.4, trend=0.7, matchup=0.5, recent_form=0.7, score=31.0, confidence=31, tier="A"),
        MlbPlayCandidate(player_id="2", player_name="Base Guy", team="NYY", opponent="BOS", game_id="g1", market="TB", line="2+ TB", stat_value=0.7, baseline=0.28, trend=0.6, matchup=0.5, recent_form=0.6, score=24.0, confidence=24, tier="B"),
        MlbPlayCandidate(player_id="3", player_name="Ks Guy", team="BOS", opponent="NYY", game_id="g1", market="K", line="6+ K", stat_value=0.7, baseline=0.4, trend=0.6, matchup=0.6, recent_form=0.7, score=26.0, confidence=26, tier="B"),
        MlbPlayCandidate(player_id="4", player_name="HR Guy", team="NYY", opponent="BOS", game_id="g1", market="HR", line="HR 1+", stat_value=0.3, baseline=0.12, trend=0.2, matchup=0.5, recent_form=0.5, score=20.0, confidence=20, tier="C"),
    ]

    top_signals = build_market_diverse_top_signals(candidates=candidates, limit=3)

    # preferred_markets order is (HR, RBI, TB, K, Hits) so TB beats Hits regardless of score
    assert [signal["market"] for signal in top_signals] == ["TB", "K", "Hits"]


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
    game_feed = {"liveData": {"boxscore": game_boxscore}}

    results = build_player_hr_results(game_feed, {"phase": "final"})

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
            "season_hr_probability": 0.31,
            "historical_hr_probability": 0.34,
            "ops": 0.945,
            "slg": 0.612,
            "iso": 0.288,
            "sample_reliability": 0.92,
            "projected_pa": 4.4,
            "age": 24,
            "order_estimate": 2,
            "recent_peak_hr_rate": 0.071,
            "unlucky_power_index": 0.22,
            "rising_star_index": 0.31,
            "pitcher_matchup": 0.64,
        },
    )

    scored = score_candidate(candidate)

    assert "L5 0.60/g" in scored.reason
    assert "HR% 0.31" in scored.reason
    assert "HistHR% 0.34" in scored.reason
    assert "OPS 0.945" in scored.reason
    assert "Age 24" in scored.reason
    assert "Order est. 2" in scored.reason
    assert "Power due 0.22" in scored.reason
    assert "Rising 0.31" in scored.reason


def test_hr_inputs_regress_tiny_samples_below_established_sluggers():
    roster = {
        "roster": [
            {
                "position": {"abbreviation": "1B"},
                "person": {
                    "id": 1,
                    "fullName": "Bench Bat",
                    "stats": [
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "season"},
                            "splits": [
                                {
                                    "stat": {
                                        "gamesPlayed": 4,
                                        "plateAppearances": 12,
                                        "homeRuns": 2,
                                        "hits": 5,
                                        "totalBases": 12,
                                        "avg": ".417",
                                        "ops": "1.300",
                                        "slg": ".917",
                                    }
                                }
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "gameLog"},
                            "splits": [
                                {"stat": {"plateAppearances": 3, "homeRuns": 1, "hits": 1, "totalBases": 4}},
                                {"stat": {"plateAppearances": 3, "homeRuns": 0, "hits": 2, "totalBases": 3}},
                                {"stat": {"plateAppearances": 3, "homeRuns": 1, "hits": 1, "totalBases": 4}},
                                {"stat": {"plateAppearances": 3, "homeRuns": 0, "hits": 1, "totalBases": 1}},
                            ],
                        },
                    ],
                },
            },
            {
                "position": {"abbreviation": "RF"},
                "person": {
                    "id": 2,
                    "fullName": "Everyday Slugger",
                    "stats": [
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "season"},
                            "splits": [
                                {
                                    "stat": {
                                        "gamesPlayed": 30,
                                        "plateAppearances": 132,
                                        "homeRuns": 9,
                                        "hits": 32,
                                        "totalBases": 63,
                                        "avg": ".267",
                                        "ops": ".881",
                                        "slg": ".525",
                                    }
                                }
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "gameLog"},
                            "splits": [
                                {"stat": {"plateAppearances": 5, "homeRuns": 1, "hits": 2, "totalBases": 5}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 0, "hits": 1, "totalBases": 1}},
                                {"stat": {"plateAppearances": 5, "homeRuns": 1, "hits": 2, "totalBases": 4}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 0, "hits": 1, "totalBases": 1}},
                                {"stat": {"plateAppearances": 5, "homeRuns": 0, "hits": 1, "totalBases": 2}},
                            ],
                        },
                    ],
                },
            },
        ]
    }

    players = build_hitter_inputs(
        roster=roster,
        team_abbr="NYY",
        opponent_abbr="BOS",
        game_id="nyy-bos-2026-05-01",
        opposing_pitcher={"era": "4.45"},
        team_hitting={"gamesPlayed": 30, "runs": 150, "obp": ".320", "ops": ".750"},
        lineup_context={"lineup_confirmed": False, "starter_ids": [], "order_by_player": {}, "status_by_player": {}},
        game_status={"phase": "pregame"},
    )
    hr_rows = {player.player_name: player for player in players if player.market == "HR"}

    assert hr_rows["Everyday Slugger"].stat_value > hr_rows["Bench Bat"].stat_value
    assert hr_rows["Everyday Slugger"].extra["sample_reliability"] > hr_rows["Bench Bat"].extra["sample_reliability"]


def test_hr_inputs_reward_proven_power_and_unlucky_slugger_profile():
    roster = {
        "roster": [
            {
                "position": {"abbreviation": "DH"},
                "person": {
                    "id": 10,
                    "fullName": "Proven Bomber",
                    "stats": [
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "season"},
                            "splits": [
                                {
                                    "stat": {
                                        "gamesPlayed": 26,
                                        "plateAppearances": 112,
                                        "homeRuns": 4,
                                        "hits": 24,
                                        "totalBases": 49,
                                        "avg": ".248",
                                        "ops": ".871",
                                        "slg": ".505",
                                    }
                                }
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "gameLog"},
                            "splits": [
                                {"stat": {"plateAppearances": 5, "homeRuns": 0, "hits": 1, "totalBases": 1}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 0, "hits": 1, "totalBases": 2}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 0, "hits": 1, "totalBases": 3}},
                                {"stat": {"plateAppearances": 5, "homeRuns": 0, "hits": 2, "totalBases": 4}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 0, "hits": 1, "totalBases": 2}},
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "career"},
                            "splits": [
                                {
                                    "stat": {
                                        "plateAppearances": 3200,
                                        "homeRuns": 185,
                                    }
                                }
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "yearByYear"},
                            "splits": [
                                {"season": "2025", "stat": {"plateAppearances": 640, "homeRuns": 42}},
                                {"season": "2024", "stat": {"plateAppearances": 618, "homeRuns": 38}},
                                {"season": "2023", "stat": {"plateAppearances": 601, "homeRuns": 36}},
                            ],
                        },
                    ],
                },
            },
            {
                "position": {"abbreviation": "LF"},
                "person": {
                    "id": 11,
                    "fullName": "Empty Pop",
                    "stats": [
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "season"},
                            "splits": [
                                {
                                    "stat": {
                                        "gamesPlayed": 26,
                                        "plateAppearances": 110,
                                        "homeRuns": 5,
                                        "hits": 26,
                                        "totalBases": 42,
                                        "avg": ".273",
                                        "ops": ".789",
                                        "slg": ".420",
                                    }
                                }
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "gameLog"},
                            "splits": [
                                {"stat": {"plateAppearances": 4, "homeRuns": 1, "hits": 1, "totalBases": 4}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 0, "hits": 1, "totalBases": 1}},
                                {"stat": {"plateAppearances": 5, "homeRuns": 1, "hits": 2, "totalBases": 5}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 0, "hits": 1, "totalBases": 1}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 0, "hits": 1, "totalBases": 1}},
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "career"},
                            "splits": [
                                {
                                    "stat": {
                                        "plateAppearances": 900,
                                        "homeRuns": 28,
                                    }
                                }
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "yearByYear"},
                            "splits": [
                                {"season": "2025", "stat": {"plateAppearances": 380, "homeRuns": 9}},
                                {"season": "2024", "stat": {"plateAppearances": 355, "homeRuns": 11}},
                                {"season": "2023", "stat": {"plateAppearances": 165, "homeRuns": 5}},
                            ],
                        },
                    ],
                },
            },
        ]
    }

    players = build_hitter_inputs(
        roster=roster,
        team_abbr="LAD",
        opponent_abbr="SF",
        game_id="lad-sf-2026-05-01",
        opposing_pitcher={"era": "4.75"},
        team_hitting={"gamesPlayed": 30, "runs": 150, "obp": ".320", "ops": ".750"},
        lineup_context={"lineup_confirmed": False, "starter_ids": [], "order_by_player": {}, "status_by_player": {}},
        game_status={"phase": "pregame"},
    )
    hr_rows = {player.player_name: player for player in players if player.market == "HR"}

    assert hr_rows["Proven Bomber"].stat_value > hr_rows["Empty Pop"].stat_value
    assert hr_rows["Proven Bomber"].extra["historical_hr_probability"] > hr_rows["Empty Pop"].extra["historical_hr_probability"]
    assert hr_rows["Proven Bomber"].extra["unlucky_power_index"] > 0.0


def test_hr_inputs_surface_rising_star_breakout_profile():
    roster = {
        "roster": [
            {
                "position": {"abbreviation": "CF"},
                "person": {
                    "id": 21,
                    "currentAge": 23,
                    "fullName": "Breakout Kid",
                    "stats": [
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "season"},
                            "splits": [
                                {
                                    "stat": {
                                        "age": 23,
                                        "gamesPlayed": 28,
                                        "plateAppearances": 118,
                                        "homeRuns": 8,
                                        "hits": 31,
                                        "totalBases": 61,
                                        "avg": ".289",
                                        "ops": ".928",
                                        "slg": ".571",
                                    }
                                }
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "gameLog"},
                            "splits": [
                                {"stat": {"plateAppearances": 5, "homeRuns": 1, "hits": 2, "totalBases": 5}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 0, "hits": 1, "totalBases": 2}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 1, "hits": 1, "totalBases": 4}},
                                {"stat": {"plateAppearances": 5, "homeRuns": 0, "hits": 2, "totalBases": 3}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 1, "hits": 2, "totalBases": 5}},
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "career"},
                            "splits": [
                                {
                                    "stat": {
                                        "plateAppearances": 280,
                                        "homeRuns": 12,
                                    }
                                }
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "yearByYear"},
                            "splits": [
                                {"season": "2025", "stat": {"plateAppearances": 150, "homeRuns": 4}},
                                {"season": "2024", "stat": {"plateAppearances": 12, "homeRuns": 0}},
                            ],
                        },
                    ],
                },
            },
            {
                "position": {"abbreviation": "1B"},
                "person": {
                    "id": 22,
                    "currentAge": 31,
                    "fullName": "Stable Vet",
                    "stats": [
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "season"},
                            "splits": [
                                {
                                    "stat": {
                                        "age": 31,
                                        "gamesPlayed": 28,
                                        "plateAppearances": 120,
                                        "homeRuns": 8,
                                        "hits": 30,
                                        "totalBases": 58,
                                        "avg": ".281",
                                        "ops": ".905",
                                        "slg": ".548",
                                    }
                                }
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "gameLog"},
                            "splits": [
                                {"stat": {"plateAppearances": 5, "homeRuns": 1, "hits": 2, "totalBases": 4}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 0, "hits": 1, "totalBases": 1}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 1, "hits": 1, "totalBases": 4}},
                                {"stat": {"plateAppearances": 5, "homeRuns": 0, "hits": 2, "totalBases": 3}},
                                {"stat": {"plateAppearances": 4, "homeRuns": 1, "hits": 1, "totalBases": 4}},
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "career"},
                            "splits": [
                                {
                                    "stat": {
                                        "plateAppearances": 3200,
                                        "homeRuns": 170,
                                    }
                                }
                            ],
                        },
                        {
                            "group": {"displayName": "hitting"},
                            "type": {"displayName": "yearByYear"},
                            "splits": [
                                {"season": "2025", "stat": {"plateAppearances": 610, "homeRuns": 29}},
                                {"season": "2024", "stat": {"plateAppearances": 635, "homeRuns": 31}},
                            ],
                        },
                    ],
                },
            },
        ]
    }

    players = build_hitter_inputs(
        roster=roster,
        team_abbr="SEA",
        opponent_abbr="TEX",
        game_id="sea-tex-2026-05-01",
        opposing_pitcher={"era": "4.65"},
        team_hitting={"gamesPlayed": 30, "runs": 150, "obp": ".320", "ops": ".750"},
        lineup_context={"lineup_confirmed": False, "starter_ids": [], "order_by_player": {}, "status_by_player": {}},
        game_status={"phase": "pregame"},
    )
    hr_rows = {player.player_name: player for player in players if player.market == "HR"}

    assert hr_rows["Breakout Kid"].extra["rising_star_index"] > 0.22
    assert hr_rows["Stable Vet"].extra["rising_star_index"] == 0.0
