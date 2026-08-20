"""NFL collector tests. The ESPN-calling layer is exercised through a
monkeypatched `espn_get_json` (no live network calls) with canned response
shapes captured from real ESPN endpoints during development; the pure
transform/candidate-assembly functions are tested directly against synthetic
stat dicts."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import pytest

from app.collectors import nfl_collector
from app.scoring.tiers import NFL_TIER_CUTOFFS, assign_nfl_tier


# ---------- flatten_core_stats -----------------------------------------------

def test_flatten_core_stats_reads_name_value_pairs():
    payload = {
        "splits": {
            "categories": [
                {"name": "general", "stats": [{"name": "gamesPlayed", "value": 17.0}]},
                {"name": "rushing", "stats": [{"name": "rushingYards", "value": 730.0}, {"name": "rushingTouchdowns", "value": 12.0}]},
            ]
        }
    }
    flat = nfl_collector.flatten_core_stats(payload)
    assert flat == {"gamesPlayed": 17.0, "rushingYards": 730.0, "rushingTouchdowns": 12.0}


def test_flatten_core_stats_handles_missing_splits():
    assert nfl_collector.flatten_core_stats({}) == {}


# ---------- extract_team_map -------------------------------------------------

def test_extract_team_map_builds_abbr_to_id():
    events = [
        {
            "competitions": [
                {
                    "competitors": [
                        {"team": {"abbreviation": "SEA", "id": "26"}},
                        {"team": {"abbreviation": "NE", "id": "17"}},
                    ]
                }
            ]
        }
    ]
    assert nfl_collector.extract_team_map(events) == {"SEA": "26", "NE": "17"}


# ---------- build_team_power_profiles ----------------------------------------

def test_build_team_power_profiles_computes_win_pct_and_point_diff():
    schedules = {
        "SEA": [
            {"game_id": "1", "won": True, "points_for": 30.0, "points_against": 10.0, "date": "2025-09-01"},
            {"game_id": "2", "won": False, "points_for": 10.0, "points_against": 20.0, "date": "2025-08-25"},
        ]
    }
    profiles = nfl_collector.build_team_power_profiles(schedules)
    assert profiles["SEA"]["win_pct"] == 0.5
    assert profiles["SEA"]["point_diff_per_game"] == 5.0  # (20 + -10) / 2
    assert isinstance(profiles["SEA"]["power"], float)


def test_build_team_power_profiles_handles_no_games():
    profiles = nfl_collector.build_team_power_profiles({"NE": []})
    assert profiles["NE"] == {"power": 0.0, "win_pct": 0.5, "point_diff_per_game": 0.0}


# ---------- build_league_baseline --------------------------------------------

def test_build_league_baseline_averages_sampled_teams_only():
    defense_allowed = {
        "SEA": {"rush_yds": 100.0, "pass_yds": 200.0, "sample": 5},
        "NE": {"rush_yds": 120.0, "pass_yds": 240.0, "sample": 5},
        "NYJ": {"rush_yds": 0.0, "pass_yds": 0.0, "sample": 0},  # excluded: no boxscore data
    }
    baseline = nfl_collector.build_league_baseline(defense_allowed)
    assert baseline["rush_yds"] == 110.0
    assert baseline["pass_yds"] == 220.0


# ---------- make_candidate / build_team_player_candidates --------------------

def test_make_candidate_assigns_nfl_tier_and_carries_lineup_flag():
    candidate = nfl_collector.make_candidate(
        player_id="123",
        player_name="Test Back",
        team="SEA",
        opponent="NE",
        game_id="401",
        market="TD",
        line="Anytime TD",
        probability=0.60,
        reason="test",
    )
    assert candidate["market"] == "TD"
    assert candidate["lineup_confirmed"] is False
    assert candidate["tier"] in {"A", "B", "C", "PASS"}
    assert 1 <= candidate["confidence"] <= 99
    assert candidate["tier"] == assign_nfl_tier(candidate["score"], "TD")


def test_build_team_player_candidates_produces_expected_markets_for_a_bellcow_rb():
    roster = [{"id": "1", "displayName": "Bellcow Back", "position": "RB", "injury_status": "ACTIVE"}]
    player_stats = {
        "1": {
            "gamesPlayed": 17.0,
            "rushingTouchdowns": 12.0,
            "receivingTouchdowns": 2.0,
            "passingTouchdowns": 0.0,
            "rushingYardsPerGame": 85.0,
            "rushingYards": 1445.0,
            "receivingYardsPerGame": 20.0,
            "receivingYards": 340.0,
            "receptions": 34.0,
        }
    }
    candidates = nfl_collector.build_team_player_candidates(
        game_id="401",
        team_abbr="SEA",
        opponent_abbr="NE",
        roster=roster,
        player_stats=player_stats,
        opponent_allowed={"rush_yds": 140.0, "pass_yds": 220.0},  # soft run defense
        league_baseline={"rush_yds": 110.0, "pass_yds": 220.0},
    )
    markets = {row["market"] for row in candidates}
    assert "TD" in markets
    assert "RushYds" in markets
    # No PassTD for a non-QB.
    assert "PassTD" not in markets
    for row in candidates:
        assert row["lineup_confirmed"] is False
        assert row["team"] == "SEA"
        assert row["opponent"] == "NE"


def test_build_team_player_candidates_skips_players_below_minimum_games():
    roster = [{"id": "2", "displayName": "Rookie", "position": "WR", "injury_status": "ACTIVE"}]
    player_stats = {"2": {"gamesPlayed": 1.0, "receivingYards": 10.0, "receptions": 1.0}}
    candidates = nfl_collector.build_team_player_candidates(
        game_id="401",
        team_abbr="SEA",
        opponent_abbr="NE",
        roster=roster,
        player_stats=player_stats,
        opponent_allowed={"rush_yds": 110.0, "pass_yds": 220.0},
        league_baseline={"rush_yds": 110.0, "pass_yds": 220.0},
    )
    assert candidates == []


def test_build_team_player_candidates_produces_pass_td_for_starting_qb():
    roster = [{"id": "3", "displayName": "Starting QB", "position": "QB", "injury_status": "ACTIVE"}]
    player_stats = {
        "3": {
            "gamesPlayed": 17.0,
            "passingTouchdowns": 28.0,
            "passingYardsPerGame": 260.0,
            "passingYards": 4420.0,
            "completionsPerGame": 24.0,
            "completions": 408.0,
            "interceptionsPerGame": 1.1,
            "interceptions": 18.7,
            "rushingTouchdowns": 2.0,
            "receivingTouchdowns": 0.0,
            "rushingYardsPerGame": 12.0,
            "rushingYards": 204.0,
            "receivingYards": 0.0,
            "receptions": 0.0,
        }
    }
    candidates = nfl_collector.build_team_player_candidates(
        game_id="401",
        team_abbr="SEA",
        opponent_abbr="NE",
        roster=roster,
        player_stats=player_stats,
        opponent_allowed={"rush_yds": 110.0, "pass_yds": 260.0},  # soft pass defense
        league_baseline={"rush_yds": 110.0, "pass_yds": 220.0},
    )
    markets = {row["market"] for row in candidates}
    assert "PassTD" in markets
    assert "PassYds" in markets
    assert "Completions" in markets
    assert "INT" in markets
    assert "REC" not in markets
    assert "RecYds" not in markets

    pass_yds_row = next(row for row in candidates if row["market"] == "PassYds")
    assert pass_yds_row["line"].endswith("+ Pass Yds")
    assert 0.0 < pass_yds_row["score"] < 100.0

    completions_row = next(row for row in candidates if row["market"] == "Completions")
    assert completions_row["line"].endswith("+ Completions")
    assert 0.0 < completions_row["score"] < 100.0

    int_rows = [row for row in candidates if row["market"] == "INT"]
    assert int_rows
    assert all(row["line"].endswith("+ INT") for row in int_rows)
    # Ladder: rungs strictly increase and probability strictly decreases,
    # same monotonic shape as the PassTD ladder.
    lines = [int(row["line"].split("+")[0]) for row in int_rows]
    assert lines == sorted(lines) == list(range(1, len(int_rows) + 1))
    scores = [row["score"] for row in int_rows]
    assert scores == sorted(scores, reverse=True)


def test_build_team_player_candidates_no_pass_yds_for_non_qb():
    roster = [{"id": "9", "displayName": "Some WR", "position": "WR", "injury_status": "ACTIVE"}]
    player_stats = {
        "9": {
            "gamesPlayed": 17.0,
            "receivingTouchdowns": 6.0,
            "receivingYardsPerGame": 70.0,
            "receivingYards": 1190.0,
            "receptions": 85.0,
            "rushingYardsPerGame": 0.0,
            "rushingYards": 0.0,
        }
    }
    candidates = nfl_collector.build_team_player_candidates(
        game_id="401",
        team_abbr="SEA",
        opponent_abbr="NE",
        roster=roster,
        player_stats=player_stats,
        opponent_allowed={"rush_yds": 110.0, "pass_yds": 260.0},
        league_baseline={"rush_yds": 110.0, "pass_yds": 220.0},
    )
    assert "PassYds" not in {row["market"] for row in candidates}
    markets = {row["market"] for row in candidates}
    assert "Completions" not in markets
    assert "INT" not in markets


def test_build_team_player_candidates_tags_rows_for_same_game_sim():
    # The same-game correlation sim (app/sim/nfl_same_game.py) reads these
    # extra fields straight off the raw candidate rows -- position for
    # pass-catcher identification, and the exact already-computed
    # pass_td_lambda for the QB rather than re-deriving it.
    roster = [
        {"id": "3", "displayName": "Starting QB", "position": "QB", "injury_status": "ACTIVE"},
        {"id": "4", "displayName": "Bellcow Back", "position": "RB", "injury_status": "ACTIVE"},
    ]
    player_stats = {
        "3": {
            "gamesPlayed": 17.0,
            "passingTouchdowns": 28.0,
            "rushingTouchdowns": 2.0,
            "receivingTouchdowns": 0.0,
            "rushingYardsPerGame": 12.0,
            "rushingYards": 204.0,
            "receivingYards": 0.0,
            "receptions": 0.0,
        },
        "4": {
            "gamesPlayed": 17.0,
            "rushingTouchdowns": 12.0,
            "receivingTouchdowns": 2.0,
            "passingTouchdowns": 0.0,
            "rushingYardsPerGame": 85.0,
            "rushingYards": 1445.0,
            "receivingYardsPerGame": 20.0,
            "receivingYards": 340.0,
            "receptions": 34.0,
        },
    }
    candidates = nfl_collector.build_team_player_candidates(
        game_id="401",
        team_abbr="SEA",
        opponent_abbr="NE",
        roster=roster,
        player_stats=player_stats,
        opponent_allowed={"rush_yds": 110.0, "pass_yds": 260.0},
        league_baseline={"rush_yds": 110.0, "pass_yds": 220.0},
    )
    qb_rows = [row for row in candidates if row["player_id"] == "3"]
    rb_rows = [row for row in candidates if row["player_id"] == "4"]
    assert qb_rows and rb_rows

    for row in qb_rows:
        assert row["position"] == "QB"
        assert row["games_played"] == 17.0
        if row["market"] == "PassTD":
            assert row["pass_td_lambda"] > 0.0

    for row in rb_rows:
        assert row["position"] == "RB"
        assert row["games_played"] == 17.0
        assert row["rec_td_per_game"] == pytest.approx(2.0 / 17.0)
        assert "pass_td_lambda" not in row


# ---------- build_moneyline_candidate ----------------------------------------

def test_build_moneyline_candidate_favors_the_stronger_team():
    candidate = nfl_collector.build_moneyline_candidate(
        game_id="401",
        away_abbr="NE",
        home_abbr="SEA",
        away_power={"power": -8.0},
        home_power={"power": 8.0},
    )
    assert candidate["team"] == "SEA"
    assert candidate["market"] == "ML"
    assert candidate["opponent"] == "NE"
    assert candidate["score"] > 50.0


# ---------- monkeypatched ESPN wiring (no live network) ----------------------

def test_fetch_target_week_uses_mocked_scoreboard(monkeypatch):
    calls = []

    def fake_get(url, params=None):
        calls.append((url, params))
        return {
            "events": [
                {
                    "id": "1",
                    "date": "2026-09-10T00:20Z",
                    "competitions": [
                        {
                            "status": {"type": {"completed": False}},
                            "competitors": [
                                {"team": {"abbreviation": "SEA", "id": "26"}},
                                {"team": {"abbreviation": "NE", "id": "17"}},
                            ],
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(nfl_collector, "espn_get_json", fake_get)
    season, week, events = nfl_collector.fetch_target_week(start_season=2026, start_week=1)
    assert season == 2026
    assert week == 1
    assert len(events) == 1
    assert calls[0][1] == {"year": 2026, "seasontype": 2, "week": 1}


def test_fetch_target_week_degrades_to_empty_when_espn_unavailable(monkeypatch):
    import requests

    def fake_get(url, params=None):
        raise requests.RequestException("boom")

    monkeypatch.setattr(nfl_collector, "espn_get_json", fake_get)
    season, week, events = nfl_collector.fetch_target_week(start_season=2026, start_week=1)
    assert events == []


def test_fetch_team_rosters_filters_to_playable_skill_positions(monkeypatch):
    def fake_get(url, params=None):
        return {
            "athletes": [
                {
                    "items": [
                        {"id": "1", "displayName": "Active RB", "position": {"abbreviation": "RB"}, "status": {"type": "active"}, "injuries": []},
                        {"id": "2", "displayName": "Out WR", "position": {"abbreviation": "WR"}, "status": {"type": "active"}, "injuries": [{"status": "Out"}]},
                        {"id": "3", "displayName": "Lineman", "position": {"abbreviation": "OT"}, "status": {"type": "active"}, "injuries": []},
                    ]
                }
            ]
        }

    monkeypatch.setattr(nfl_collector, "espn_get_json", fake_get)
    rosters = nfl_collector.fetch_team_rosters({"SEA": "26"})
    names = {row["displayName"] for row in rosters["SEA"]}
    assert names == {"Active RB"}


# ---------- NFL tier cutoffs --------------------------------------------------

def test_nfl_tier_cutoffs_cover_every_market():
    assert set(NFL_TIER_CUTOFFS) == {"TD", "REC", "RushYds", "RecYds", "PassTD", "PassYds", "Completions", "INT", "ML"}


def test_assign_nfl_tier_uses_market_specific_scale():
    # 50 clears REC's B cutoff (50) but not TD's A cutoff (45 < 50, so TD *does* hit A).
    assert assign_nfl_tier(50.0, "REC") == "B"
    assert assign_nfl_tier(50.0, "TD") == "A"
    assert assign_nfl_tier(5.0, "ML") == "PASS"
    assert assign_nfl_tier(90.0, "ML") == "A"


def test_assign_nfl_tier_falls_back_to_default_scale_for_unknown_market():
    assert assign_nfl_tier(40.0, "Unknown") == "A"
