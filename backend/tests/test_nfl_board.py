"""NFL pure-predictions tests.

Entirely offline: every test builds its own synthetic raw payload, so nothing
here touches ESPN or the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.builders.nfl_board_builder import (
    apply_anti_correlation,
    build_market_diverse_top_signals,
    build_section_boards,
)
from app.collectors.nfl_collector import parse_gamelog_event, player_key, ppr_points
from app.models.nfl_model import (
    build_nfl_candidates,
    find_value_line,
    matchup_ratio_for,
    round_to_step,
)
from app.sim.sim_engine import simulate_candidates


# ------------------------------------------------------------------ fixtures


def make_log(*, index: int, opponent: str = "SF", **stats) -> dict:
    log = {
        "event_id": f"e{index}",
        "season": 2026,
        "game_date": f"2026-10-{index + 1:02d}T17:00:00+00:00",
        "is_home": index % 2 == 0,
        "opponent": opponent,
        "PASS_ATT": 0.0, "PASS_CMP": 0.0, "PASS_YDS": 0.0, "PASS_TD": 0.0, "INT": 0.0,
        "CARRIES": 0.0, "RUSH_YDS": 0.0, "RUSH_TD": 0.0,
        "REC": 0.0, "TARGETS": 0.0, "REC_YDS": 0.0, "REC_TD": 0.0,
        "FUM_LOST": 0.0, "TD": 0.0, "TOTAL_TD": 0.0, "snap_load": 0.0, "fantasy_ppr": 0.0,
    }
    log.update(stats)
    return log


def make_profile(*, player_id: str, name: str, position: str, team: str, logs: list[dict], snap_load: float) -> dict:
    from app.collectors.nfl_collector import average_log_block

    return {
        "player_id": player_id,
        "player_name": name,
        "player_key": player_key(name),
        "team": team,
        "position": position,
        "injury_status": "ACTIVE",
        "logs": logs,
        "seasons": [2026],
        "season_avgs": average_log_block(logs),
        "l10_avgs": average_log_block(logs[:10]),
        "l5_avgs": average_log_block(logs[:5]),
        "games_played": len(logs),
        "snap_load": snap_load,
    }


def make_raw_payload(*, spread: float | None = -3.0, over_under: float | None = 45.5) -> dict:
    qb_logs = [
        make_log(index=i, PASS_ATT=34, PASS_YDS=255 + (i * 6), PASS_TD=1.6, snap_load=34, fantasy_ppr=18.0)
        for i in range(8)
    ]
    rb_logs = [
        make_log(index=i, CARRIES=17, RUSH_YDS=72 + (i * 3), RUSH_TD=0.5, REC=3, REC_YDS=22,
                 TD=1 if i % 2 == 0 else 0, snap_load=20, fantasy_ppr=15.5)
        for i in range(8)
    ]
    wr_logs = [
        make_log(index=i, REC=5, TARGETS=8, REC_YDS=68 + (i * 4), REC_TD=0.4,
                 TD=1 if i % 3 == 0 else 0, snap_load=8, fantasy_ppr=13.0)
        for i in range(8)
    ]
    wr2_logs = [
        make_log(index=i, REC=4, TARGETS=6, REC_YDS=52, REC_TD=0.3,
                 TD=1 if i % 4 == 0 else 0, snap_load=6, fantasy_ppr=10.0)
        for i in range(8)
    ]

    return {
        "sport": "NFL",
        "date": "2026-10-11",
        "season_year": 2026,
        "season_type": "Regular Season",
        "week": 6,
        "games": [
            {
                "game_id": "401700001",
                "away_team": "SF",
                "home_team": "KC",
                "away_record": 0.6,
                "home_record": 0.8,
                "time": "Sun 4:25 PM ET",
                "spread": spread,
                "over_under": over_under,
                "indoor": False,
            }
        ],
        "player_profiles": {
            "1": make_profile(player_id="1", name="Test Quarterback", position="QB", team="KC", logs=qb_logs, snap_load=34),
            "2": make_profile(player_id="2", name="Test Runner", position="RB", team="KC", logs=rb_logs, snap_load=20),
            "3": make_profile(player_id="3", name="Test Receiver", position="WR", team="KC", logs=wr_logs, snap_load=8),
            "4": make_profile(player_id="4", name="Second Receiver", position="WR", team="KC", logs=wr2_logs, snap_load=6),
        },
        "history_profiles": {},
        "history_seasons": [2026, 2025, 2024],
        "defense_profiles": {
            "KC": {"allowed_points": 19.0, "allowed_pass_yds": 210.0, "allowed_rush_yds": 95.0,
                   "allowed_total_yds": 305.0, "recent_win_pct": 0.8, "sample": 5},
            "SF": {"allowed_points": 25.0, "allowed_pass_yds": 260.0, "allowed_rush_yds": 130.0,
                   "allowed_total_yds": 390.0, "recent_win_pct": 0.4, "sample": 5},
        },
        "allowance_baselines": {"points": 22.0, "pass_yds": 235.0, "rush_yds": 112.0, "total_yds": 347.0},
    }


# --------------------------------------------------------------- line search


def test_value_line_lands_near_a_coin_flip():
    logs = [make_log(index=i, REC_YDS=yards) for i, yards in enumerate([40, 55, 60, 75, 80, 95, 100, 110])]

    valued = find_value_line(market="REC_YDS", recent_logs=logs, baseline=76.0, projection=78.0)

    assert valued is not None
    # The quoted line is the one whose shrunken hit rate sits closest to 0.50 —
    # not the chalkiest line available.
    assert abs(valued["hit_rate"] - 0.50) <= 0.12
    assert valued["line"] % 10 == 0, "receiving yards quote on a 10-yard grid"


def test_line_search_walks_the_market_grid():
    logs = [make_log(index=i, PASS_YDS=240 + i * 10) for i in range(8)]

    valued = find_value_line(market="PASS_YDS", recent_logs=logs, baseline=265.0, projection=270.0)

    assert valued is not None
    assert valued["line"] % 25 == 0, "passing yards quote in 25-yard steps"
    assert valued["line"] >= 150


def test_round_to_step_never_returns_below_one_step():
    assert round_to_step(3.0, 25) == 25
    assert round_to_step(260.0, 25) == 250
    assert round_to_step(2.4, 1) == 2


# ------------------------------------------------------------------- matchup


def test_matchup_ratio_rewards_a_generous_defense_and_stays_clamped():
    generous = {"allowed_pass_yds": 300.0}
    stingy = {"allowed_pass_yds": 150.0}
    baselines = {"pass_yds": 235.0}

    assert matchup_ratio_for(market="PASS_YDS", opponent_defense=generous, baselines=baselines) == 1.15
    assert matchup_ratio_for(market="PASS_YDS", opponent_defense=stingy, baselines=baselines) == 0.85
    # No data is neutral, never a penalty.
    assert matchup_ratio_for(market="PASS_YDS", opponent_defense={}, baselines=baselines) == 1.0


# ---------------------------------------------------------------- candidates


def test_candidates_respect_position_eligibility():
    candidates = build_nfl_candidates(make_raw_payload())

    by_player: dict[str, set[str]] = {}
    for candidate in candidates:
        by_player.setdefault(candidate["player_name"], set()).add(candidate["market"])

    assert "PASS_YDS" in by_player["Test Quarterback"]
    assert "REC" not in by_player["Test Quarterback"], "quarterbacks don't have reception markets"
    assert "PASS_YDS" not in by_player["Test Receiver"]
    assert {"REC", "REC_YDS"} <= by_player["Test Receiver"]


def test_every_candidate_carries_game_script_context():
    candidates = build_nfl_candidates(make_raw_payload(spread=-3.0))

    home = [row for row in candidates if row["team"] == "KC" and row["market"] != "ML"]
    assert home, "expected home candidates"
    for candidate in home:
        # Home team is favored by 3, so its own spread stays negative.
        assert candidate["spread"] == -3.0
        assert candidate["over_under"] == 45.5
        assert candidate["indoor"] is False


def test_spread_is_flipped_for_the_away_team():
    payload = make_raw_payload(spread=-6.5)
    payload["player_profiles"]["9"] = make_profile(
        player_id="9",
        name="Away Runner",
        position="RB",
        team="SF",
        logs=[make_log(index=i, opponent="KC", CARRIES=16, RUSH_YDS=70, TD=1 if i % 2 else 0, snap_load=18) for i in range(8)],
        snap_load=18,
    )

    candidates = build_nfl_candidates(payload)
    away = [row for row in candidates if row["player_name"] == "Away Runner"]

    assert away
    # Home favored by 6.5 means the visitor is a 6.5-point underdog.
    assert all(row["spread"] == 6.5 for row in away)


def test_moneyline_is_quoted_once_per_game():
    candidates = build_nfl_candidates(make_raw_payload())
    moneylines = [row for row in candidates if row["market"] == "ML"]

    assert len(moneylines) == 1
    assert moneylines[0]["team"] in {"KC", "SF"}
    assert moneylines[0]["opponent"] != moneylines[0]["team"]


def test_anti_correlation_demotes_the_second_receiver():
    candidates = [
        {"game_id": "g1", "team": "KC", "market": "REC_YDS", "player_name": "WR1", "score": 70.0,
         "confidence": 70, "reason": "base"},
        {"game_id": "g1", "team": "KC", "market": "REC_YDS", "player_name": "WR2", "score": 68.0,
         "confidence": 68, "reason": "base"},
    ]

    apply_anti_correlation(candidates)

    assert candidates[0]["score"] == 70.0, "the leader is untouched"
    assert candidates[1]["score"] == 66.75
    assert "shares touches with WR1" in candidates[1]["reason"]


def test_anti_correlation_leaves_passing_alone():
    # Two quarterbacks on one team don't split a target pool the way pass
    # catchers do, so PASS_YDS is deliberately outside the contested set.
    candidates = [
        {"game_id": "g1", "team": "KC", "market": "PASS_YDS", "player_name": "QB1", "score": 70.0,
         "confidence": 70, "reason": "base"},
        {"game_id": "g1", "team": "KC", "market": "PASS_YDS", "player_name": "QB2", "score": 60.0,
         "confidence": 60, "reason": "base"},
    ]

    apply_anti_correlation(candidates)

    assert [row["score"] for row in candidates] == [70.0, 60.0]


# ---------------------------------------------------------------- simulation


def test_game_script_moves_passing_and_rushing_in_opposite_directions():
    def build(market: str, spread: float, total: float) -> dict:
        return {
            "player_id": f"{market}-{spread}",
            "market": market,
            "line": "250+ PASS YDS" if market == "PASS_YDS" else "60+ RUSH YDS",
            "l10_hit_rate": 0.55,
            "l5_hit_rate": 0.55,
            "spread": spread,
            "over_under": total,
            "indoor": True,
        }

    trailing_pass = build("PASS_YDS", 7.0, 50.0)
    leading_pass = build("PASS_YDS", -7.0, 40.0)
    trailing_rush = build("RUSH_YDS", 7.0, 50.0)
    leading_rush = build("RUSH_YDS", -7.0, 40.0)

    simulate_candidates([trailing_pass, leading_pass, trailing_rush, leading_rush], sport="NFL")

    assert trailing_pass["sim_prob"] > leading_pass["sim_prob"], "underdogs throw more"
    assert leading_rush["sim_prob"] > trailing_rush["sim_prob"], "favorites run more"


def test_ladders_are_monotone_and_reproduce_the_headline_rung():
    candidate = {
        "player_id": "ladder-1",
        "market": "REC_YDS",
        "line": "60+ REC YDS",
        "l10_hit_rate": 0.52,
        "l5_hit_rate": 0.58,
    }

    simulate_candidates([candidate], sport="NFL")
    ladder = candidate["ladder"]

    rungs = sorted(ladder)
    assert rungs == [40, 50, 60, 75, 100]
    assert all(ladder[low] >= ladder[high] for low, high in zip(rungs, rungs[1:]))
    assert ladder[60] == candidate["sim_prob"], "the headline rung is the simulated probability"


def test_touchdown_ladder_uses_a_count_process():
    candidate = {"player_id": "td-1", "market": "TD", "line": "1+ TD", "l10_hit_rate": 0.5, "l5_hit_rate": 0.5}

    simulate_candidates([candidate], sport="NFL")
    ladder = candidate["ladder"]

    assert sorted(ladder) == [1, 2]
    assert ladder[1] == candidate["sim_prob"]
    # Two touchdowns is strictly rarer than one, and not vanishingly so for a
    # coin-flip anytime scorer.
    assert 0.0 < ladder[2] < ladder[1]


# -------------------------------------------------------------------- boards


def test_section_boards_cover_every_prop_market():
    candidates = build_nfl_candidates(make_raw_payload())
    simulate_candidates(candidates, sport="NFL")

    boards = build_section_boards(candidates, 10)

    assert set(boards) == {"PASS_YDS", "RUSH_YDS", "REC_YDS", "REC", "TD"}
    assert boards["PASS_YDS"]["title"] == "Passing Board"
    for board in boards.values():
        assert all(row["market"] == board["market"] for row in board["players"])


def test_top_signals_diversify_markets_and_players():
    candidates = [
        {"market": "TD", "player_name": "A", "line": "1+ TD", "score": 80.0, "confidence": 80, "tier": "A"},
        {"market": "REC_YDS", "player_name": "B", "line": "60+ REC YDS", "score": 79.0, "confidence": 79, "tier": "A"},
        {"market": "TD", "player_name": "B", "line": "1+ TD", "score": 78.0, "confidence": 78, "tier": "A"},
        {"market": "RUSH_YDS", "player_name": "C", "line": "60+ RUSH YDS", "score": 70.0, "confidence": 70, "tier": "B"},
    ]

    signals = build_market_diverse_top_signals(candidates=candidates, limit=3)

    assert [signal["market"] for signal in signals] == ["TD", "REC_YDS", "RUSH_YDS"]
    assert len({signal["player_name"] for signal in signals}) == 3


# ----------------------------------------------------------------- pipeline


def test_pipeline_builds_validates_and_exports(tmp_path, monkeypatch):
    """End-to-end with the network stubbed out.

    This is the check that matters for the hourly loop: the board the builder
    produces has to satisfy the strict BoardPayload schema and land in all
    three export locations, or CI ships a broken file.
    """
    import app.builders.nfl_board_builder as builder
    from app.main import run_nfl_pipeline

    monkeypatch.setattr(builder, "collect_nfl_raw_data", lambda *args, **kwargs: make_raw_payload())

    board = run_nfl_pipeline(tmp_path)

    assert board["sport"] == "NFL"
    assert board["week"] == 6
    assert [category["key"] for category in board["categories"]] == ["pure_predictions", "salary_historical"]
    assert board["pinned_board"]["market"] == "TD"
    assert board["games"], "expected at least one game"
    assert set(board["games"][0]["markets"]) == {"PASS_YDS", "RUSH_YDS", "REC_YDS", "REC", "TD", "ML"}
    # No contracts file exists under the temp root, so the salary category
    # degrades with a stated reason instead of failing the whole build.
    assert board["salary_board"]["available"] is False
    assert board["salary_board"]["reason"]
    for location in ("backend/data_final", "frontend/data", "data"):
        assert (tmp_path / location / "nfl.json").exists(), f"missing export in {location}"


def test_pipeline_keeps_the_last_good_board_when_the_slate_is_empty(tmp_path, monkeypatch):
    import app.builders.nfl_board_builder as builder
    from app.main import run_nfl_pipeline

    monkeypatch.setattr(builder, "collect_nfl_raw_data", lambda *args, **kwargs: make_raw_payload())
    populated = run_nfl_pipeline(tmp_path)
    assert populated["games"]

    empty_payload = make_raw_payload()
    empty_payload["games"] = []
    empty_payload["player_profiles"] = {}
    monkeypatch.setattr(builder, "collect_nfl_raw_data", lambda *args, **kwargs: empty_payload)

    board = run_nfl_pipeline(tmp_path)

    # A Tuesday with no posted slate must not wipe Sunday's board.
    assert board["games"]


# ------------------------------------------------------------------ collector


def test_gamelog_parser_reads_espn_stat_names():
    log = parse_gamelog_event(
        stats=["24/35", "289", "2", "1", "4", "31", "0", "1"],
        names=[
            "completions/passingAttempts", "passingYards", "passingTouchdowns", "interceptions",
            "rushingAttempts", "rushingYards", "rushingTouchdowns", "fumblesLost",
        ],
        metadata={"id": "401700009", "gameDate": "2026-10-11T17:00Z", "atVs": "@", "opponent": {"abbreviation": "SF"}},
        season=2026,
    )

    assert log is not None
    assert log["PASS_CMP"] == 24 and log["PASS_ATT"] == 35
    assert log["PASS_YDS"] == 289
    assert log["RUSH_YDS"] == 31
    assert log["is_home"] is False
    assert log["opponent"] == "SF"
    # 289*0.04 + 2*4 - 1*2 + 31*0.1 - 1*2 = 11.56 + 8 - 2 + 3.1 - 2
    assert log["fantasy_ppr"] == 18.66


def test_gamelog_parser_rejects_a_misaligned_row():
    assert parse_gamelog_event(
        stats=["24/35", "289"],
        names=["completions/passingAttempts", "passingYards", "passingTouchdowns"],
        metadata={"id": "1", "gameDate": "2026-10-11T17:00Z"},
        season=2026,
    ) is None


def test_ppr_scoring_matches_the_standard_formula():
    assert ppr_points(
        pass_yds=0, pass_td=0, interceptions=0,
        rush_yds=100, rush_td=1, receptions=5, rec_yds=50, rec_td=0, fumbles_lost=0,
    ) == 26.0


def test_player_key_normalizes_suffixes_and_punctuation():
    assert player_key("Marvin Harrison Jr.") == player_key("marvin harrison jr")
    assert player_key("Ja'Marr Chase") == "ja marr chase"
    assert player_key("Michael Penix Jr.") == "michael penix"
