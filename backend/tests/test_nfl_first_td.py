"""Pure-math tests for the NFL first-TD scorer board (app.sim.nfl_first_td)
against fixed synthetic candidate lists -- no live network calls, same
style as test_nfl_qb_stack.py / test_nfl_rb_stack.py.

The load-bearing property is the Poisson-race identity: within a game, the
modeled players' first-TD probabilities plus the unmodeled residual's share
must account for exactly P(the game produces a touchdown at all) -- no
more, no less. A first-TD board whose rows quietly sum past that is
overstating every row on it.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import pytest

from app.sim import nfl_first_td as ftd


def _candidates():
    """Two teams' worth of rows shaped like the collector's tagged output:
    td_lambda repeated across every row a player produces, which is exactly
    the deduplication case collect_scorer_lambdas has to handle."""
    rows = []
    roster = [
        ("sea_rb", "Sea Back", "SEA", "SF", "RB", 0.80),
        ("sea_wr", "Sea Wideout", "SEA", "SF", "WR", 0.45),
        ("sea_te", "Sea Tight End", "SEA", "SF", "TE", 0.25),
        ("sf_rb", "Niner Back", "SF", "SEA", "RB", 0.70),
        ("sf_wr", "Niner Wideout", "SF", "SEA", "WR", 0.50),
    ]
    for player_id, name, team, opponent, position, lam in roster:
        for market, line in (("TD", "Anytime TD"), ("RushYds", "45+ Rush Yds")):
            rows.append({
                "player_id": player_id, "player_name": name, "team": team,
                "opponent": opponent, "position": position, "market": market,
                "line": line, "td_lambda": lam,
            })
    return rows


def _game():
    return ftd.build_first_td_for_game(
        game_id="401", matchup="SEA @ SF", time="4:25 PM ET",
        candidates=_candidates(),
    )


def test_collect_scorer_lambdas_dedupes_repeated_player_rows():
    scorers = ftd.collect_scorer_lambdas(_candidates())
    assert len(scorers) == 5
    assert sorted(scorer["player_id"] for scorer in scorers) == [
        "sea_rb", "sea_te", "sea_wr", "sf_rb", "sf_wr",
    ]
    assert {scorer["td_lambda"] for scorer in scorers} == {0.80, 0.45, 0.25, 0.70, 0.50}


def test_rows_without_a_usable_lambda_are_skipped():
    rows = [
        {"player_id": "a", "player_name": "No Lambda", "team": "SEA", "market": "REC"},
        {"player_id": "b", "player_name": "Zero", "team": "SEA", "market": "TD", "td_lambda": 0.0},
        {"player_id": "c", "player_name": "Text", "team": "SEA", "market": "TD", "td_lambda": "n/a"},
        {"player_id": "d", "player_name": "Real", "team": "SEA", "market": "TD", "td_lambda": 0.5},
    ]
    assert [scorer["player_id"] for scorer in ftd.collect_scorer_lambdas(rows)] == ["d"]


def test_unmodeled_lambda_produces_exactly_the_configured_share():
    modeled = 4.0
    other = ftd.unmodeled_lambda(modeled, share=0.10)
    assert other / (modeled + other) == pytest.approx(0.10)


def test_first_event_probability_is_intensity_share_times_any_event():
    probability = ftd.first_event_probability(1.0, 4.0)
    assert probability == pytest.approx(0.25 * (1 - math.exp(-4.0)))


def test_first_event_probability_degrades_to_zero_on_an_empty_race():
    assert ftd.first_event_probability(0.0, 0.0) == 0.0
    assert ftd.first_event_probability(1.0, 0.0) == 0.0


def test_game_first_td_probabilities_reconcile_to_p_at_least_one_td():
    """Every modeled player's share plus the unmodeled residual's share must
    add up to P(the game produces a touchdown), not to 1.0 and not to some
    number above it."""
    game = _game()
    total_lambda = game["total_lambda"]
    modeled = sum(row["first_td_prob_pct"] for row in game["players"]) / 100
    residual_lambda = total_lambda - game["modeled_lambda"]
    residual = ftd.first_event_probability(residual_lambda, total_lambda)
    any_td = 1 - math.exp(-total_lambda)
    # Tolerance is set by the board's own rounding, not by the math: every
    # exported probability is a 2dp percentage and total_lambda a 3dp float,
    # so the reconciliation can only be checked to about 1e-4 off the wire.
    assert modeled + residual == pytest.approx(any_td, abs=1e-4)
    assert game["no_td_prob_pct"] == pytest.approx(math.exp(-total_lambda) * 100, abs=0.01)


def test_the_residual_haircut_actually_lowers_every_modeled_row():
    """Without the unmodeled bucket a player would be priced at his share of
    the modeled lambda alone. The board must price him strictly below that."""
    game = _game()
    modeled_lambda = game["modeled_lambda"]
    for row in game["players"]:
        no_haircut = ftd.first_event_probability(row["td_lambda"], modeled_lambda)
        assert row["first_td_prob_pct"] / 100 < no_haircut


def test_team_first_td_is_always_likelier_than_the_game_first_td():
    """A player scoring his own team's first touchdown is a strictly weaker
    condition than scoring the whole game's first one -- the board's two
    prices must never invert."""
    for row in _game()["players"]:
        assert row["team_first_td_prob_pct"] > row["first_td_prob_pct"]


def test_team_rows_split_the_whole_opening_between_the_two_sidelines():
    game = _game()
    opens = sum(team["opens_scoring_prob_pct"] for team in game["teams"]) / 100
    assert opens == pytest.approx(1 - math.exp(-game["total_lambda"]), abs=1e-4)
    assert {team["team"] for team in game["teams"]} == {"SEA", "SF"}


def test_the_higher_lambda_team_is_favored_to_open_the_scoring():
    game = _game()
    assert game["teams"][0]["team"] == "SEA"  # 1.50 total lambda vs SF's 1.20
    assert game["teams"][0]["top_scorer"] == "Sea Back"


def test_players_are_ranked_by_first_td_probability():
    probabilities = [row["first_td_prob_pct"] for row in _game()["players"]]
    assert probabilities == sorted(probabilities, reverse=True)


def test_fair_odds_track_the_probability_direction():
    game = _game()
    best, worst = game["players"][0], game["players"][-1]
    assert int(best["first_td_fair_odds"]) < int(worst["first_td_fair_odds"])


def test_a_game_with_no_modelable_scorer_is_skipped_not_raised():
    assert ftd.build_first_td_for_game(
        game_id="402", matchup="X @ Y", time="1:00 PM ET",
        candidates=[{"player_id": "a", "player_name": "A", "team": "X", "market": "REC"}],
    ) is None


def test_parlays_never_stack_two_legs_from_the_same_game():
    """Two first-TD legs in one game are mutually exclusive, so a parlay that
    pairs them is a dead ticket, not a long shot."""
    games = [
        ftd.build_first_td_for_game(game_id=str(index), matchup=f"A{index} @ B{index}",
                                    time="1:00 PM ET", candidates=_candidates())
        for index in range(4)
    ]
    parlays = ftd.build_first_td_parlays(games)
    for tickets in parlays.values():
        for ticket in tickets:
            matchups = [leg["matchup"] for leg in ticket["legs"]]
            assert len(set(matchups)) == len(matchups)


def test_parlay_joint_probability_is_the_product_of_its_legs():
    games = [
        ftd.build_first_td_for_game(game_id=str(index), matchup=f"A{index} @ B{index}",
                                    time="1:00 PM ET", candidates=_candidates())
        for index in range(3)
    ]
    ticket = ftd.build_first_td_parlays(games)["3_leg"][0]
    product = 1.0
    for leg in ticket["legs"]:
        product *= leg["prob_pct"] / 100
    assert ticket["joint_prob_pct"] == pytest.approx(product * 100, abs=1e-3)
    assert ticket["decimal"] == pytest.approx(round(1 / product, 1), abs=0.2)


def test_board_shape_is_present_and_empty_off_season():
    board = ftd.build_first_td_board([])
    assert board["top_board"] == [] and board["team_board"] == [] and board["games"] == []
    assert board["parlays"] == {"2_leg": [], "3_leg": []}
    assert board["assumptions"] and board["method"]


def test_board_top_and_team_boards_are_slate_wide_and_sorted():
    games = [
        ftd.build_first_td_for_game(game_id=str(index), matchup=f"A{index} @ B{index}",
                                    time="1:00 PM ET", candidates=_candidates())
        for index in range(3)
    ]
    board = ftd.build_first_td_board(games)
    top = [row["first_td_prob_pct"] for row in board["top_board"]]
    teams = [row["opens_scoring_prob_pct"] for row in board["team_board"]]
    assert top == sorted(top, reverse=True)
    assert teams == sorted(teams, reverse=True)
    assert len(board["top_board"]) <= ftd.TOP_BOARD_LIMIT
