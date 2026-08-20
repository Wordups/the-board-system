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

    # Multi-TD ladder: a real bellcow's rate should clear at least a 2+ TD
    # rung, not just the anytime (1+) floor.
    td_rows = [row for row in candidates if row["market"] == "TD"]
    assert any(row["line"] == "Anytime TD" for row in td_rows)
    assert any(row["line"] == "2+ TD" for row in td_rows)
    lines = [row["line"] for row in td_rows]
    thresholds = [1 if line == "Anytime TD" else int(line.split("+")[0]) for line in lines]
    assert thresholds == sorted(thresholds)
    scores = [row["score"] for row in td_rows]
    assert scores == sorted(scores, reverse=True)


def test_build_team_player_candidates_td_ladder_rung_one_unchanged_by_laddering():
    # Rung 1's probability/gate must be byte-identical to the pre-ladder
    # behavior -- laddering is purely additive on top of it.
    roster = [{"id": "1", "displayName": "Modest Back", "position": "RB", "injury_status": "ACTIVE"}]
    player_stats = {
        "1": {
            "gamesPlayed": 17.0,
            "rushingTouchdowns": 3.0,
            "receivingTouchdowns": 0.0,
            "rushingYardsPerGame": 40.0,
            "rushingYards": 680.0,
            "receivingYardsPerGame": 5.0,
            "receivingYards": 85.0,
            "receptions": 10.0,
        }
    }
    candidates = nfl_collector.build_team_player_candidates(
        game_id="401", team_abbr="SEA", opponent_abbr="NE", roster=roster, player_stats=player_stats,
        opponent_allowed={"rush_yds": 110.0, "pass_yds": 220.0},
        league_baseline={"rush_yds": 110.0, "pass_yds": 220.0},
    )
    td_rows = [row for row in candidates if row["market"] == "TD"]
    assert len(td_rows) == 1
    assert td_rows[0]["line"] == "Anytime TD"


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
        starting_qb_id="3",
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


# ---------- starting QB gating (regression: only one QB's markets built) -----

def _qb_competition_roster_fixture():
    # A real recurring shape: a genuine QB competition where multiple young
    # arms each started several 2025 games. Rattler/Shough/Wilson (all
    # New Orleans, 2025) is the real case this was caught against.
    roster = [
        {"id": "qb1", "displayName": "Arm One", "position": "QB", "injury_status": "ACTIVE"},
        {"id": "qb2", "displayName": "Arm Two", "position": "QB", "injury_status": "ACTIVE"},
        {"id": "qb3", "displayName": "Arm Three", "position": "QB", "injury_status": "ACTIVE"},
    ]
    player_stats = {
        "qb1": {"gamesPlayed": 10.0, "passingTouchdowns": 14.0, "passingYardsPerGame": 220.0, "completions": 210.0, "passingAttempts": 320.0},
        "qb2": {"gamesPlayed": 6.0, "passingTouchdowns": 8.0, "passingYardsPerGame": 200.0, "completions": 120.0, "passingAttempts": 190.0},
        "qb3": {"gamesPlayed": 4.0, "passingTouchdowns": 5.0, "passingYardsPerGame": 180.0, "completions": 75.0, "passingAttempts": 120.0},
    }
    return roster, player_stats


def test_find_starting_qb_id_picks_most_games_played():
    roster, player_stats = _qb_competition_roster_fixture()
    assert nfl_collector.find_starting_qb_id(roster, player_stats) == "qb1"


def test_find_starting_qb_id_none_when_no_qualifying_qb():
    roster = [{"id": "qb1", "displayName": "Deep Third", "position": "QB", "injury_status": "ACTIVE"}]
    player_stats = {"qb1": {"gamesPlayed": 1.0, "passingTouchdowns": 0.0}}
    assert nfl_collector.find_starting_qb_id(roster, player_stats) is None


def test_build_team_player_candidates_only_starting_qb_gets_markets():
    # The actual bug: without starting_qb_id, all three qualifying QBs
    # produced full PassTD/PassYds/Completions/INT lines for the same team
    # in the same game, as if a team could have three simultaneous starters.
    roster, player_stats = _qb_competition_roster_fixture()
    candidates = nfl_collector.build_team_player_candidates(
        game_id="401", team_abbr="NO", opponent_abbr="DET", roster=roster, player_stats=player_stats,
        opponent_allowed={"rush_yds": 110.0, "pass_yds": 220.0},
        league_baseline={"rush_yds": 110.0, "pass_yds": 220.0},
        starting_qb_id=nfl_collector.find_starting_qb_id(roster, player_stats),
    )
    qb_ids_with_markets = {row["player_id"] for row in candidates if row["market"] in ("PassTD", "PassYds", "Completions", "INT")}
    assert qb_ids_with_markets == {"qb1"}
    # The backups shouldn't be on the board at all for this game, not even
    # in a lesser role (see find_starting_qb_id's docstring).
    assert not any(row["player_id"] in ("qb2", "qb3") for row in candidates)


def test_build_team_player_candidates_no_starting_qb_id_excludes_all_qbs():
    # starting_qb_id defaults to None (e.g. a team with zero qualifying QBs
    # this call didn't compute one for) - must exclude every QB, not
    # silently let one through.
    roster, player_stats = _qb_competition_roster_fixture()
    candidates = nfl_collector.build_team_player_candidates(
        game_id="401", team_abbr="NO", opponent_abbr="DET", roster=roster, player_stats=player_stats,
        opponent_allowed={"rush_yds": 110.0, "pass_yds": 220.0},
        league_baseline={"rush_yds": 110.0, "pass_yds": 220.0},
    )
    assert candidates == []


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
        starting_qb_id="3",
    )
    qb_rows = [row for row in candidates if row["player_id"] == "3"]
    rb_rows = [row for row in candidates if row["player_id"] == "4"]
    assert qb_rows and rb_rows

    for row in qb_rows:
        assert row["position"] == "QB"
        assert row["games_played"] == 17.0
        if row["market"] == "PassTD":
            assert row["pass_td_lambda"] > 0.0
        # pass_yds_mean/completions_mean are tagged onto every QB row (not
        # just PassYds/Completions rows) same unconditional-tag convention
        # as pass_td_lambda -- the QB stat-stack sim (app/sim/nfl_qb_stack.py)
        # reads these off the specific PassYds/Completions market rows.
        assert row["pass_yds_mean"] > 0.0
        assert row["completions_mean"] > 0.0

    for row in rb_rows:
        assert row["position"] == "RB"
        assert row["games_played"] == 17.0
        assert row["rec_td_per_game"] == pytest.approx(2.0 / 17.0)
        assert "pass_td_lambda" not in row
        assert "pass_yds_mean" not in row
        assert "completions_mean" not in row


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


# ---------- parse_receiving_lines --------------------------------------------

def _boxscore_players_fixture(*, include_targets: bool = True) -> dict:
    """Synthetic boxscore.players fixture: two teams, each with a receiving
    category. Team 100 = WR-heavy (one WR eats most of the work), team 200 =
    TE-heavy (soft against TEs), mirroring a real ESPN summary response
    shape as reasoned from the well-documented public boxscore format."""
    labels = ["REC", "YDS", "AVG", "TD", "LONG"]
    if include_targets:
        labels = labels + ["TGTS"]

    def athlete_row(athlete_id, position, rec, yds, td, targets):
        stats = [str(rec), str(yds), "0.0", str(td), "0"]
        if include_targets:
            stats.append(str(targets))
        return {"athlete": {"id": athlete_id, "position": {"abbreviation": position}}, "stats": stats}

    return {
        "boxscore": {
            "teams": [
                {"team": {"id": "100"}, "statistics": [{"name": "rushingYards", "value": "90"}, {"name": "netPassingYards", "value": "210"}]},
                {"team": {"id": "200"}, "statistics": [{"name": "rushingYards", "value": "80"}, {"name": "netPassingYards", "value": "230"}]},
            ],
            "players": [
                {
                    "team": {"id": "100"},
                    "statistics": [
                        {
                            "name": "receiving",
                            "labels": labels,
                            "athletes": [
                                athlete_row("11", "WR", 9, 130, 1, 12),
                                athlete_row("12", "TE", 2, 20, 0, 3),
                                athlete_row("13", "RB", 3, 25, 0, 4),
                            ],
                        }
                    ],
                },
                {
                    "team": {"id": "200"},
                    "statistics": [
                        {
                            "name": "receiving",
                            "labels": labels,
                            "athletes": [
                                athlete_row("21", "WR", 3, 40, 0, 5),
                                athlete_row("22", "TE", 8, 110, 2, 10),
                                athlete_row("23", "RB", 2, 15, 0, 3),
                            ],
                        }
                    ],
                },
            ],
        }
    }


def test_parse_receiving_lines_extracts_stat_lines_by_label():
    summary = _boxscore_players_fixture()
    lines = nfl_collector.parse_receiving_lines(summary)
    assert lines is not None
    assert len(lines) == 6
    wr_line = next(row for row in lines if row["athlete_id"] == "11")
    assert wr_line["team_id"] == "100"
    assert wr_line["position"] == "WR"
    assert wr_line["rec"] == 9.0
    assert wr_line["rec_yds"] == 130.0
    assert wr_line["rec_td"] == 1.0
    assert wr_line["targets"] == 12.0


def test_parse_receiving_lines_falls_back_to_receptions_when_targets_absent():
    summary = _boxscore_players_fixture(include_targets=False)
    lines = nfl_collector.parse_receiving_lines(summary)
    assert lines is not None
    wr_line = next(row for row in lines if row["athlete_id"] == "11")
    # No TGTS column in labels -> targets is None on the parsed line itself;
    # the rec-count fallback happens one level up in
    # summarize_receiving_by_position (see test below).
    assert wr_line["targets"] is None
    assert wr_line["rec"] == 9.0


def test_parse_receiving_lines_returns_none_when_players_breakdown_missing():
    summary = {"boxscore": {"teams": [{"team": {"id": "100"}, "statistics": []}]}}
    assert nfl_collector.parse_receiving_lines(summary) is None


def test_parse_receiving_lines_returns_none_for_falsy_summary():
    assert nfl_collector.parse_receiving_lines(None) is None
    assert nfl_collector.parse_receiving_lines({}) is None


# ---------- summarize_receiving_by_position -----------------------------------

def test_summarize_receiving_by_position_splits_own_vs_opponent():
    summary = _boxscore_players_fixture()
    lines = nfl_collector.parse_receiving_lines(summary)

    own = nfl_collector.summarize_receiving_by_position(
        lines, team_id_filter="100", include_own_team=True, athlete_positions={}
    )
    assert own["WR"]["rec_yds"] == 130.0
    assert own["TE"]["rec_yds"] == 20.0
    assert own["RB"]["rec_yds"] == 25.0

    opponent = nfl_collector.summarize_receiving_by_position(
        lines, team_id_filter="100", include_own_team=False, athlete_positions={}
    )
    # Opponent of team 100 is team 200's lines.
    assert opponent["TE"]["rec_yds"] == 110.0
    assert opponent["WR"]["rec_yds"] == 40.0


def test_summarize_receiving_by_position_falls_back_to_receptions_for_targets():
    summary = _boxscore_players_fixture(include_targets=False)
    lines = nfl_collector.parse_receiving_lines(summary)
    own = nfl_collector.summarize_receiving_by_position(
        lines, team_id_filter="100", include_own_team=True, athlete_positions={}
    )
    # No TGTS column anywhere -> targets falls back to rec count.
    assert own["WR"]["targets"] == 9.0
    assert own["TE"]["targets"] == 2.0


def test_summarize_receiving_by_position_prefers_roster_position_map():
    summary = _boxscore_players_fixture()
    lines = nfl_collector.parse_receiving_lines(summary)
    # Athlete 11 is tagged WR in the boxscore, but the roster map says RB --
    # the roster map should win.
    own = nfl_collector.summarize_receiving_by_position(
        lines, team_id_filter="100", include_own_team=True, athlete_positions={"11": "RB"}
    )
    assert own["RB"]["rec_yds"] == 130.0 + 25.0  # athlete 11 + athlete 13


# ---------- fetch_defense_allowed: position split + graceful degrade ---------

def _team_schedules_fixture():
    return {
        "SEA": [{"game_id": "g1", "team_id": "100", "date": "2025-09-01", "won": True, "points_for": 24.0, "points_against": 17.0}],
        "NE": [{"game_id": "g1", "team_id": "200", "date": "2025-09-01", "won": False, "points_for": 17.0, "points_against": 24.0}],
    }


def test_fetch_defense_allowed_computes_position_split_from_summaries():
    team_map = {"SEA": "100", "NE": "200"}
    schedules = _team_schedules_fixture()
    summaries = {"g1": _boxscore_players_fixture()}

    allowed = nfl_collector.fetch_defense_allowed(team_map, schedules, summaries, athlete_positions={})

    # Team-level signal unchanged/still computed from boxscore.teams.
    assert allowed["SEA"]["rush_yds"] == 80.0  # SEA's opponent (NE, team 200) rushed for 80
    assert allowed["SEA"]["pass_yds"] == 230.0

    # SEA's defense (team 100) allowed NE's (team 200) receiving lines --
    # NE's TE went off (110 yds), NE's WR was quiet (40 yds).
    sea_split = allowed["SEA"]["rec_allowed_by_position"]
    assert sea_split["TE"]["rec_yds"] == 110.0
    assert sea_split["TE"]["sample"] == 1
    assert sea_split["WR"]["rec_yds"] == 40.0

    # NE's defense (team 200) allowed SEA's (team 100) receiving lines --
    # SEA's WR went off (130 yds).
    ne_split = allowed["NE"]["rec_allowed_by_position"]
    assert ne_split["WR"]["rec_yds"] == 130.0
    assert ne_split["TE"]["rec_yds"] == 20.0


def test_fetch_defense_allowed_degrades_gracefully_without_players_breakdown():
    """A summary with only boxscore.teams (no boxscore.players) -- the shape
    this collector used exclusively before this change -- must still produce
    correct team-level numbers, with the position split reporting an honest
    zero-sample rather than crashing or fabricating a value."""
    team_map = {"SEA": "100", "NE": "200"}
    schedules = _team_schedules_fixture()
    summaries = {
        "g1": {
            "boxscore": {
                "teams": [
                    {"team": {"id": "100"}, "statistics": [{"name": "rushingYards", "value": "90"}, {"name": "netPassingYards", "value": "210"}]},
                    {"team": {"id": "200"}, "statistics": [{"name": "rushingYards", "value": "80"}, {"name": "netPassingYards", "value": "230"}]},
                ]
                # No "players" key at all.
            }
        }
    }

    allowed = nfl_collector.fetch_defense_allowed(team_map, schedules, summaries, athlete_positions={})
    assert allowed["SEA"]["rush_yds"] == 80.0
    assert allowed["SEA"]["pass_yds"] == 230.0
    for position in ("WR", "TE", "RB"):
        assert allowed["SEA"]["rec_allowed_by_position"][position]["sample"] == 0
        assert allowed["SEA"]["rec_allowed_by_position"][position]["rec_yds"] == 0.0


# ---------- fetch_target_shares -----------------------------------------------

def test_fetch_target_shares_computes_share_by_position():
    team_map = {"SEA": "100", "NE": "200"}
    schedules = _team_schedules_fixture()
    summaries = {"g1": _boxscore_players_fixture()}

    shares = nfl_collector.fetch_target_shares(team_map, schedules, summaries, athlete_positions={})

    sea_shares = shares["SEA"]
    # SEA (team 100) targets: WR 12, TE 3, RB 4 -> total 19.
    assert sea_shares["WR"] == pytest.approx(12 / 19, rel=1e-3)
    assert sea_shares["TE"] == pytest.approx(3 / 19, rel=1e-3)
    assert sea_shares["RB"] == pytest.approx(4 / 19, rel=1e-3)
    assert sum(sea_shares[p] for p in ("WR", "TE", "RB")) == pytest.approx(1.0)
    assert sea_shares["sample"] == 1


def test_fetch_target_shares_falls_back_to_receptions_when_targets_absent():
    team_map = {"SEA": "100", "NE": "200"}
    schedules = _team_schedules_fixture()
    summaries = {"g1": _boxscore_players_fixture(include_targets=False)}

    shares = nfl_collector.fetch_target_shares(team_map, schedules, summaries, athlete_positions={})
    sea_shares = shares["SEA"]
    # No TGTS column -> falls back to receptions: WR 9, TE 2, RB 3 -> total 14.
    assert sea_shares["WR"] == pytest.approx(9 / 14, rel=1e-3)


def test_fetch_target_shares_degrades_to_empty_when_no_players_data():
    team_map = {"SEA": "100", "NE": "200"}
    schedules = _team_schedules_fixture()
    summaries = {"g1": {"boxscore": {"teams": []}}}

    shares = nfl_collector.fetch_target_shares(team_map, schedules, summaries, athlete_positions={})
    assert shares["SEA"] == {"sample": 0}


# ---------- build_league_baseline_by_position ---------------------------------

def test_build_league_baseline_by_position_excludes_zero_sample_teams():
    defense_allowed = {
        "SEA": {"rush_yds": 100.0, "pass_yds": 200.0, "sample": 1, "rec_allowed_by_position": {
            "WR": {"rec_yds": 100.0, "rec": 6.0, "rec_td": 1.0, "sample": 1},
            "TE": {"rec_yds": 30.0, "rec": 3.0, "rec_td": 0.0, "sample": 1},
            "RB": {"rec_yds": 20.0, "rec": 2.0, "rec_td": 0.0, "sample": 1},
        }},
        "NE": {"rush_yds": 90.0, "pass_yds": 220.0, "sample": 1, "rec_allowed_by_position": {
            "WR": {"rec_yds": 60.0, "rec": 4.0, "rec_td": 0.0, "sample": 1},
            "TE": {"rec_yds": 0.0, "rec": 0.0, "rec_td": 0.0, "sample": 0},
            "RB": {"rec_yds": 10.0, "rec": 1.0, "rec_td": 0.0, "sample": 1},
        }},
    }
    baseline = nfl_collector.build_league_baseline_by_position(defense_allowed)
    assert baseline["WR"]["rec_yds"] == 80.0  # average of 100 and 60
    assert baseline["TE"]["rec_yds"] == 30.0  # NE excluded (sample 0)
    assert baseline["RB"]["rec_yds"] == 15.0


# ---------- compute_position_matchups ------------------------------------------

def test_compute_position_matchups_prefers_position_specific_when_sample_present():
    opponent_allowed = {
        "pass_yds": 220.0,
        "rec_allowed_by_position": {
            "WR": {"rec_yds": 40.0, "sample": 3},   # stout vs WR
            "TE": {"rec_yds": 90.0, "sample": 3},   # soft vs TE
            "RB": {"rec_yds": 20.0, "sample": 3},
        },
    }
    league_baseline_by_position = {
        "WR": {"rec_yds": 80.0},
        "TE": {"rec_yds": 45.0},
        "RB": {"rec_yds": 20.0},
    }
    matchups = nfl_collector.compute_position_matchups(
        opponent_allowed=opponent_allowed, league_baseline_by_position=league_baseline_by_position, fallback=1.05
    )
    assert matchups["WR"] < 1.0  # tougher than average for WRs
    assert matchups["TE"] > 1.0  # softer than average for TEs
    assert matchups["WR"] != matchups["TE"]


def test_compute_position_matchups_falls_back_when_sample_missing():
    opponent_allowed = {"pass_yds": 220.0, "rec_allowed_by_position": {}}
    matchups = nfl_collector.compute_position_matchups(
        opponent_allowed=opponent_allowed, league_baseline_by_position={}, fallback=1.07
    )
    assert matchups == {"WR": 1.07, "TE": 1.07, "RB": 1.07}


# ---------- build_team_player_candidates: position-aware rec_matchup ----------

def test_build_team_player_candidates_diverges_wr_vs_te_matchup():
    """The core demonstration: a defense that's stout against WRs but soft
    against TEs must produce two different rec_matchup-driven scores for an
    otherwise-identical WR/TE statline, whereas the old blanket pass_matchup
    would have applied the exact same number to both."""
    roster = [
        {"id": "31", "displayName": "Possession TE", "position": "TE", "injury_status": "ACTIVE"},
        {"id": "32", "displayName": "Boundary WR", "position": "WR", "injury_status": "ACTIVE"},
    ]
    # Identical underlying rate for both players so any score difference is
    # attributable purely to the matchup multiplier.
    identical_stats = {
        "gamesPlayed": 17.0,
        "receivingTouchdowns": 6.0,
        "receivingYardsPerGame": 55.0,
        "receivingYards": 935.0,
        "receptions": 70.0,
        "rushingYardsPerGame": 0.0,
        "rushingYards": 0.0,
    }
    player_stats = {"31": dict(identical_stats), "32": dict(identical_stats)}

    opponent_allowed = {
        "rush_yds": 110.0,
        "pass_yds": 220.0,
        "rec_allowed_by_position": {
            "WR": {"rec_yds": 40.0, "rec": 3.0, "rec_td": 0.2, "sample": 5},  # stout vs WR
            "TE": {"rec_yds": 90.0, "rec": 7.0, "rec_td": 0.8, "sample": 5},  # soft vs TE
            "RB": {"rec_yds": 20.0, "rec": 2.0, "rec_td": 0.1, "sample": 5},
        },
    }
    league_baseline = {"rush_yds": 110.0, "pass_yds": 220.0}
    league_baseline_by_position = {
        "WR": {"rec_yds": 80.0, "rec": 5.0, "rec_td": 0.4},
        "TE": {"rec_yds": 45.0, "rec": 3.5, "rec_td": 0.3},
        "RB": {"rec_yds": 20.0, "rec": 2.0, "rec_td": 0.1},
    }

    candidates = nfl_collector.build_team_player_candidates(
        game_id="401",
        team_abbr="NE",
        opponent_abbr="SEA",
        roster=roster,
        player_stats=player_stats,
        opponent_allowed=opponent_allowed,
        league_baseline=league_baseline,
        league_baseline_by_position=league_baseline_by_position,
        target_share_by_position={"WR": 0.60, "TE": 0.25, "RB": 0.15},
    )

    te_rec_yds = next(row for row in candidates if row["player_id"] == "31" and row["market"] == "RecYds")
    wr_rec_yds = next(row for row in candidates if row["player_id"] == "32" and row["market"] == "RecYds")

    # Same underlying rate + same opponent, but the TE row must reflect the
    # softer TE matchup while the WR row reflects the tougher WR matchup --
    # under the old blanket pass_matchup these two would have been identical.
    assert "Rec match" in te_rec_yds["reason"]
    te_ratio = float(te_rec_yds["reason"].split("Rec match ")[1].split("x")[0])
    wr_ratio = float(wr_rec_yds["reason"].split("Rec match ")[1].split("x")[0])
    assert te_ratio > 1.0
    assert wr_ratio < 1.0
    assert te_ratio != wr_ratio
    assert te_rec_yds["score"] > wr_rec_yds["score"]

    # target_share_pg tagging, part 2 of the task -- present, not wired into
    # score math (already proven above: the score diverges purely off the
    # matchup ratio, independent of target share).
    assert te_rec_yds["target_share_pg"] == 0.25
    assert wr_rec_yds["target_share_pg"] == 0.60


def test_build_team_player_candidates_still_works_without_new_optional_params():
    """Backward compatibility: existing callers (and the pre-existing test
    suite above) that don't pass league_baseline_by_position/
    target_share_by_position must be unaffected -- rec_matchup falls back to
    the old blanket pass_matchup, and no target_share_pg key is added."""
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
    rec_yds_row = next(row for row in candidates if row["market"] == "RecYds")
    pass_matchup = nfl_model_matchup_ratio_for_test(260.0, 220.0)
    assert f"Rec match {pass_matchup:.2f}x" in rec_yds_row["reason"]
    assert "target_share_pg" not in rec_yds_row


def nfl_model_matchup_ratio_for_test(allowed_value: float, league_avg_allowed: float) -> float:
    from app.models import nfl_model

    return nfl_model.matchup_ratio(allowed_value, league_avg_allowed)


# ---------- RB trend watch (parse_rb_gamelog / build_rb_trend_watch) --------

def _gamelog_fixture(weekly_rush_rec: list[tuple[int, float, float]]) -> dict:
    """Synthetic ESPN gamelog payload shaped like the real
    site.web.api.espn.com response verified live during development:
    top-level `names` positionally matches each event's `stats` array, real
    per-game metadata (week) lives in the separate top-level `events` dict
    keyed by eventId, and `seasonTypes` holds a Postseason entry ahead of the
    Regular Season entry (real ESPN order for a team that made the
    playoffs) -- parse_rb_gamelog must select Regular Season specifically,
    not just take the first seasonTypes entry."""
    names = [
        "rushingAttempts", "rushingYards", "yardsPerRushAttempt", "rushingTouchdowns", "longRushing",
        "receptions", "receivingTargets", "receivingYards", "yardsPerReception", "receivingTouchdowns",
        "longReception", "fumbles", "fumblesLost", "fumblesForced", "kicksBlocked",
    ]
    events = {}
    reg_events = []
    for week, rush_yds, rec_yds in weekly_rush_rec:
        event_id = f"e{week}"
        events[event_id] = {"id": event_id, "week": week}
        stats = ["15", str(rush_yds), "4.0", "0", "20", "3", "4", str(rec_yds), "6.0", "0", "10", "0", "0", "-", "-"]
        reg_events.append({"eventId": event_id, "stats": stats})
    return {
        "names": names,
        "events": events,
        "seasonTypes": [
            {
                "displayName": "2025 Postseason",
                "categories": [{"events": [{"eventId": "ep1", "stats": ["1"] * 15}]}],
            },
            {
                "displayName": "2025 Regular Season",
                "categories": [{"events": reg_events}],
            },
        ],
    }


def test_parse_rb_gamelog_sorts_chronologically_and_sums_total_yards():
    payload = _gamelog_fixture([(3, 50.0, 10.0), (1, 80.0, 20.0), (2, 60.0, 5.0)])
    games = nfl_collector.parse_rb_gamelog(payload)
    assert [g["week"] for g in games] == [1, 2, 3]
    assert games[0] == {"week": 1, "rush_yds": 80.0, "rec_yds": 20.0, "total_yds": 100.0}


def test_parse_rb_gamelog_ignores_postseason_entry():
    payload = _gamelog_fixture([(1, 80.0, 20.0)])
    games = nfl_collector.parse_rb_gamelog(payload)
    # Postseason fixture event (eventId "ep1") must not leak into the result.
    assert all(g["week"] != 1 or g["total_yds"] == 100.0 for g in games)
    assert len(games) == 1


def test_parse_rb_gamelog_returns_none_when_no_regular_season_entry():
    payload = {"names": ["rushingYards", "receivingYards"], "events": {}, "seasonTypes": []}
    assert nfl_collector.parse_rb_gamelog(payload) is None


def test_parse_rb_gamelog_returns_none_when_stat_names_missing():
    payload = {"names": ["fumbles"], "events": {}, "seasonTypes": [{"displayName": "2025 Regular Season", "categories": [{"events": []}]}]}
    assert nfl_collector.parse_rb_gamelog(payload) is None


def test_fetch_rb_gamelogs_filters_to_rb_position_and_degrades_per_player(monkeypatch):
    rosters = {
        "SEA": [
            {"id": "10", "displayName": "Star RB", "position": "RB", "injury_status": "ACTIVE"},
            {"id": "11", "displayName": "Some WR", "position": "WR", "injury_status": "ACTIVE"},
        ],
        "NE": [
            {"id": "20", "displayName": "Broken RB", "position": "RB", "injury_status": "ACTIVE"},
        ],
    }
    import requests

    fixture = _gamelog_fixture([(week, 60.0 + week, 10.0) for week in range(1, 9)])

    def fake_get(url, params=None):
        assert "gamelog" in url
        if "10" in url.split("/"):
            return fixture
        raise requests.RequestException("boom")

    monkeypatch.setattr(nfl_collector, "espn_get_json", fake_get)
    result = nfl_collector.fetch_rb_gamelogs(rosters, season=2025)
    assert set(result.keys()) == {"10"}
    assert result["10"]["team"] == "SEA"
    assert len(result["10"]["games"]) == 8


def test_build_rb_trend_watch_best_stretch_finds_max_rolling_window():
    rosters = {"SEA": [{"id": "10", "displayName": "Star RB", "position": "RB", "injury_status": "ACTIVE"}]}
    # Weeks 1-8: a flat ~50/gm baseline except a hot 5-game window (weeks 3-7)
    # averaging much higher -- the max rolling-5 window should land there.
    weekly = [(1, 40.0, 10.0), (2, 45.0, 10.0), (3, 90.0, 20.0), (4, 100.0, 15.0), (5, 110.0, 10.0),
              (6, 95.0, 20.0), (7, 105.0, 15.0), (8, 40.0, 10.0)]
    gamelogs = {"10": {"team": "SEA", "games": nfl_collector.parse_rb_gamelog(_gamelog_fixture(weekly))}}
    trend = nfl_collector.build_rb_trend_watch(gamelogs, rosters)
    assert trend["window"] == 5
    assert len(trend["best_stretch"]) == 1
    row = trend["best_stretch"][0]
    assert row["player_name"] == "Star RB"
    assert row["team"] == "SEA"
    assert row["best_stretch_weeks"] == "Wk 3-7"
    # (90+20)+(100+15)+(110+10)+(95+20)+(105+15) = 580 total / 5 games
    assert row["best_stretch_avg_yds"] == 116.0


def test_build_rb_trend_watch_trending_up_compares_final_window_to_season():
    rosters = {"SEA": [{"id": "10", "displayName": "Finisher RB", "position": "RB", "injury_status": "ACTIVE"}]}
    # Cold start, hot finish: final 5 games clearly outperform the season avg.
    weekly = [(1, 20.0, 0.0), (2, 20.0, 0.0), (3, 20.0, 0.0),
              (4, 100.0, 20.0), (5, 100.0, 20.0), (6, 100.0, 20.0), (7, 100.0, 20.0), (8, 100.0, 20.0)]
    gamelogs = {"10": {"team": "SEA", "games": nfl_collector.parse_rb_gamelog(_gamelog_fixture(weekly))}}
    trend = nfl_collector.build_rb_trend_watch(gamelogs, rosters)
    assert len(trend["trending_up"]) == 1
    row = trend["trending_up"][0]
    assert row["recent_weeks"] == "Wk 4-8"
    assert row["recent_avg_total_yds"] == 120.0
    assert row["trend_pct"] > 0


def test_build_rb_trend_watch_excludes_backs_below_minimum_games():
    rosters = {"SEA": [{"id": "10", "displayName": "Small Sample RB", "position": "RB", "injury_status": "ACTIVE"}]}
    weekly = [(1, 100.0, 20.0), (2, 100.0, 20.0), (3, 100.0, 20.0), (4, 100.0, 20.0), (5, 100.0, 20.0)]
    gamelogs = {"10": {"team": "SEA", "games": nfl_collector.parse_rb_gamelog(_gamelog_fixture(weekly))}}
    trend = nfl_collector.build_rb_trend_watch(gamelogs, rosters)
    assert trend["best_stretch"] == []
    assert trend["trending_up"] == []


def test_build_rb_trend_watch_excludes_low_usage_trend_noise():
    # A rarely-used back going from 4 to 10 yds/g is a +150% trend_pct on
    # pure noise, not a real signal — must not appear regardless of how
    # large the percentage swing is.
    rosters = {"SEA": [{"id": "10", "displayName": "Deep Bench RB", "position": "RB", "injury_status": "ACTIVE"}]}
    weekly = [(1, 2.0, 0.0), (2, 3.0, 0.0), (3, 2.0, 0.0),
              (4, 6.0, 2.0), (5, 5.0, 3.0), (6, 6.0, 2.0), (7, 5.0, 3.0), (8, 6.0, 2.0)]
    gamelogs = {"10": {"team": "SEA", "games": nfl_collector.parse_rb_gamelog(_gamelog_fixture(weekly))}}
    trend = nfl_collector.build_rb_trend_watch(gamelogs, rosters)
    assert trend["trending_up"] == []


def test_build_rb_trend_watch_includes_real_role_growth_above_usage_floor():
    # A genuine complementary-back breakout (season ~22 yds/g -> recent
    # ~45 yds/g) must still appear — the floor targets noise, not real signal.
    rosters = {"SEA": [{"id": "10", "displayName": "Emerging RB", "position": "RB", "injury_status": "ACTIVE"}]}
    weekly = [(1, 18.0, 4.0), (2, 20.0, 3.0), (3, 22.0, 5.0),
              (4, 40.0, 5.0), (5, 42.0, 6.0), (6, 38.0, 7.0), (7, 44.0, 6.0), (8, 40.0, 6.0)]
    gamelogs = {"10": {"team": "SEA", "games": nfl_collector.parse_rb_gamelog(_gamelog_fixture(weekly))}}
    trend = nfl_collector.build_rb_trend_watch(gamelogs, rosters)
    assert len(trend["trending_up"]) == 1
    assert trend["trending_up"][0]["recent_avg_total_yds"] >= nfl_collector.RB_TREND_MIN_USAGE_YDS


def test_build_rb_trend_watch_excludes_backs_trending_down():
    rosters = {"SEA": [{"id": "10", "displayName": "Fading RB", "position": "RB", "injury_status": "ACTIVE"}]}
    weekly = [(1, 100.0, 20.0), (2, 100.0, 20.0), (3, 100.0, 20.0),
              (4, 20.0, 0.0), (5, 20.0, 0.0), (6, 20.0, 0.0), (7, 20.0, 0.0), (8, 20.0, 0.0)]
    gamelogs = {"10": {"team": "SEA", "games": nfl_collector.parse_rb_gamelog(_gamelog_fixture(weekly))}}
    trend = nfl_collector.build_rb_trend_watch(gamelogs, rosters)
    assert trend["trending_up"] == []
    # Best stretch still populates (it doesn't require a positive trend).
    assert len(trend["best_stretch"]) == 1


def test_build_rb_trend_watch_ranks_and_caps_at_top_n():
    rosters = {"SEA": [
        {"id": str(i), "displayName": f"RB {i}", "position": "RB", "injury_status": "ACTIVE"} for i in range(12)
    ]}
    gamelogs = {}
    for i in range(12):
        base = 50.0 + i * 5.0  # each RB slightly better than the last
        weekly = [(week, base, 10.0) for week in range(1, 9)]
        gamelogs[str(i)] = {"team": "SEA", "games": nfl_collector.parse_rb_gamelog(_gamelog_fixture(weekly))}
    trend = nfl_collector.build_rb_trend_watch(gamelogs, rosters)
    assert len(trend["best_stretch"]) == nfl_collector.RB_TREND_TOP_N
    # Highest base (RB 11) should rank first.
    assert trend["best_stretch"][0]["player_name"] == "RB 11"
    scores = [row["best_stretch_avg_yds"] for row in trend["best_stretch"]]
    assert scores == sorted(scores, reverse=True)


# ---------- QB game history (parse_qb_gamelog / fetch_qb_gamelogs) ----------

def _qb_gamelog_fixture(weekly: list[tuple[int, str, str, float, float, float, float, float]]) -> dict:
    """Synthetic ESPN gamelog payload shaped like the real
    site.web.api.espn.com QB response verified live 2026-08-19 against a
    real QB (Lamar Jackson, athlete id 3916387, 2025 season): the real
    `names[]` array returned was exactly ["completions", "passingAttempts",
    "passingYards", "completionPct", "yardsPerPassAttempt",
    "passingTouchdowns", "interceptions", "longPassing", "sacks",
    "QBRating", "adjQBR", "rushingAttempts", "rushingYards",
    "yardsPerRushAttempt", "rushingTouchdowns", "longRushing"] -- a
    completely different stat set/order than the RB gamelog fixture above,
    which is exactly why parse_qb_gamelog needs its own parser rather than
    reusing parse_rb_gamelog. weekly rows are
    (week, opponent_abbr, result, completions, pass_yds, pass_td, int, rush_yds)."""
    names = [
        "completions", "passingAttempts", "passingYards", "completionPct", "yardsPerPassAttempt",
        "passingTouchdowns", "interceptions", "longPassing", "sacks", "QBRating", "adjQBR",
        "rushingAttempts", "rushingYards", "yardsPerRushAttempt", "rushingTouchdowns", "longRushing",
    ]
    events = {}
    reg_events = []
    for week, opponent, result, completions, pass_yds, pass_td, interceptions, rush_yds in weekly:
        event_id = f"e{week}"
        events[event_id] = {
            "id": event_id,
            "week": week,
            "opponent": {"abbreviation": opponent},
            "gameResult": result,
        }
        stats = [
            str(completions), "25", str(pass_yds), "61.1", "8.4", str(pass_td), str(interceptions),
            "64", "3", "100.0", "50.0", "9", str(rush_yds), "2.3", "0", "12",
        ]
        reg_events.append({"eventId": event_id, "stats": stats})
    return {
        "names": names,
        "events": events,
        "seasonTypes": [
            {
                "displayName": "2025 Postseason",
                "categories": [{"events": [{"eventId": "ep1", "stats": ["1"] * 16}]}],
            },
            {
                "displayName": "2025 Regular Season",
                "categories": [{"events": reg_events}],
            },
        ],
    }


def test_parse_qb_gamelog_sorts_chronologically_and_reads_real_field_names():
    payload = _qb_gamelog_fixture([
        (3, "CIN", "L", 20, 180.0, 1.0, 1.0, 15.0),
        (1, "BUF", "L", 14, 209.0, 2.0, 0.0, 70.0),
        (2, "CLE", "W", 19, 225.0, 4.0, 0.0, 13.0),
    ])
    games = nfl_collector.parse_qb_gamelog(payload)
    assert [g["week"] for g in games] == [1, 2, 3]
    assert games[0] == {
        "week": 1,
        "opponent": "BUF",
        "result": "L",
        "completions": 14.0,
        "pass_yds": 209.0,
        "pass_td": 2.0,
        "interceptions": 0.0,
        "rush_yds": 70.0,
    }


def test_parse_qb_gamelog_ignores_postseason_entry():
    payload = _qb_gamelog_fixture([(1, "BUF", "L", 14, 209.0, 2.0, 0.0, 70.0)])
    games = nfl_collector.parse_qb_gamelog(payload)
    assert len(games) == 1
    assert games[0]["opponent"] == "BUF"


def test_parse_qb_gamelog_returns_none_when_no_regular_season_entry():
    payload = {"names": ["passingYards"], "events": {}, "seasonTypes": []}
    assert nfl_collector.parse_qb_gamelog(payload) is None


def test_parse_qb_gamelog_returns_none_when_stat_names_missing():
    payload = {
        "names": ["fumbles"],
        "events": {},
        "seasonTypes": [{"displayName": "2025 Regular Season", "categories": [{"events": []}]}],
    }
    assert nfl_collector.parse_qb_gamelog(payload) is None


def test_fetch_qb_gamelogs_filters_to_qb_position_and_degrades_per_player(monkeypatch):
    rosters = {
        "BAL": [
            {"id": "3916387", "displayName": "Star QB", "position": "QB", "injury_status": "ACTIVE"},
            {"id": "11", "displayName": "Some WR", "position": "WR", "injury_status": "ACTIVE"},
        ],
        "NE": [
            {"id": "20", "displayName": "Broken QB", "position": "QB", "injury_status": "ACTIVE"},
        ],
    }
    import requests

    fixture = _qb_gamelog_fixture([
        (week, "PIT", "W", 20, 220.0 + week, 2.0, 0.0, 10.0) for week in range(1, 9)
    ])

    def fake_get(url, params=None):
        assert "gamelog" in url
        if "3916387" in url.split("/"):
            return fixture
        raise requests.RequestException("boom")

    monkeypatch.setattr(nfl_collector, "espn_get_json", fake_get)
    result = nfl_collector.fetch_qb_gamelogs(rosters, season=2025)
    assert set(result.keys()) == {"3916387"}
    assert result["3916387"]["team"] == "BAL"
    assert len(result["3916387"]["games"]) == 8
    assert result["3916387"]["games"][0]["opponent"] == "PIT"
