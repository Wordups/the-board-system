"""Pure-math tests for the NFL same-game QB<->receiver correlation sim
(compound Poisson + multinomial attribution) against fixed synthetic
candidate lists — no live network calls, mirrors test_nfl_model.py's style.

The flagship test (`test_build_same_game_pairs_...`) is the point of this
module: it proves the simulated joint probability is genuinely different
from naive P(A)*P(B) independence math, which is the whole reason this is a
real Monte Carlo rather than two multiplied marginals.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import numpy as np
import pytest

from app.models import nfl_model
from app.sim import nfl_same_game as sg


def _synthetic_candidates():
    """One QB (PassTD ladder 1+/2+/3+, lambda 1.6) and three pass-catchers
    of varying receiving-TD volume on the same team, shaped like the tagged
    rows build_team_player_candidates() now produces."""
    return [
        {"player_id": "qb1", "player_name": "Test QB", "team": "SEA", "market": "PassTD",
         "line": "1+ Pass TD", "pass_td_lambda": 1.6, "position": "QB",
         "games_played": 17.0, "rec_td_per_game": 0.0},
        {"player_id": "qb1", "player_name": "Test QB", "team": "SEA", "market": "PassTD",
         "line": "2+ Pass TD", "pass_td_lambda": 1.6, "position": "QB",
         "games_played": 17.0, "rec_td_per_game": 0.0},
        {"player_id": "qb1", "player_name": "Test QB", "team": "SEA", "market": "PassTD",
         "line": "3+ Pass TD", "pass_td_lambda": 1.6, "position": "QB",
         "games_played": 17.0, "rec_td_per_game": 0.0},
        {"player_id": "wr_a", "player_name": "Alpha WR", "team": "SEA", "market": "TD",
         "line": "Anytime TD", "position": "WR", "games_played": 17.0, "rec_td_per_game": 0.7},
        {"player_id": "te_b", "player_name": "Beta TE", "team": "SEA", "market": "TD",
         "line": "Anytime TD", "position": "TE", "games_played": 17.0, "rec_td_per_game": 0.3},
        {"player_id": "rb_c", "player_name": "Gamma RB", "team": "SEA", "market": "RushYds",
         "line": "40+ Rush Yds", "position": "RB", "games_played": 17.0, "rec_td_per_game": 0.15},
    ]


# ---------- extract_qb_ladder -------------------------------------------------

def test_extract_qb_ladder_reads_floor_and_ceiling():
    qb = sg.extract_qb_ladder(_synthetic_candidates())
    assert qb["player_id"] == "qb1"
    assert qb["pass_td_lambda"] == pytest.approx(1.6)
    assert qb["rungs"] == [1, 2, 3]


def test_extract_qb_ladder_returns_none_without_pass_td_rows():
    non_qb_only = [c for c in _synthetic_candidates() if c["market"] != "PassTD"]
    assert sg.extract_qb_ladder(non_qb_only) is None


# ---------- extract_pass_catchers ---------------------------------------------

def test_extract_pass_catchers_excludes_qb_and_dedupes_by_player():
    candidates = _synthetic_candidates()
    # Duplicate one catcher's row (as would happen if a player had two market
    # candidates) to prove dedup-by-player_id holds.
    candidates.append(dict(candidates[3]))
    catchers = sg.extract_pass_catchers(candidates, exclude_player_id="qb1")
    ids = {c["player_id"] for c in catchers}
    assert ids == {"wr_a", "te_b", "rb_c"}
    assert "qb1" not in ids


def test_extract_pass_catchers_ignores_non_skill_positions():
    candidates = _synthetic_candidates() + [
        {"player_id": "ol_d", "player_name": "Lineman", "team": "SEA", "market": "TD",
         "line": "Anytime TD", "position": "OT", "games_played": 17.0, "rec_td_per_game": 0.0},
    ]
    catchers = sg.extract_pass_catchers(candidates, exclude_player_id="qb1")
    assert "ol_d" not in {c["player_id"] for c in catchers}


# ---------- catcher_attribution_weights ---------------------------------------

def test_catcher_attribution_weights_matches_hand_computed_shrunk_rate():
    catchers = sg.extract_pass_catchers(_synthetic_candidates(), exclude_player_id="qb1")
    weights = sg.catcher_attribution_weights(catchers)
    assert weights.sum() == pytest.approx(1.0)

    # Hand-computed shrunk rates: (observed*17 + prior*4) / 21
    expected_raw = {
        "wr_a": (0.7 * 17 + nfl_model.POSITION_PRIORS["WR"]["rec_td"] * 4) / 21,
        "te_b": (0.3 * 17 + nfl_model.POSITION_PRIORS["TE"]["rec_td"] * 4) / 21,
        "rb_c": (0.15 * 17 + nfl_model.POSITION_PRIORS["RB"]["rec_td"] * 4) / 21,
    }
    total = sum(expected_raw.values())
    by_id = {c["player_id"]: w for c, w in zip(catchers, weights)}
    for player_id, raw in expected_raw.items():
        assert by_id[player_id] == pytest.approx(raw / total, abs=1e-6)

    # The higher-volume WR should get the largest attribution share.
    assert by_id["wr_a"] > by_id["te_b"] > by_id["rb_c"]


def test_catcher_attribution_weights_falls_back_to_uniform_when_all_zero():
    catchers = [
        {"player_id": "a", "position": "WR", "games_played": 0.0, "rec_td_per_game": 0.0},
        {"player_id": "b", "position": "WR", "games_played": 0.0, "rec_td_per_game": 0.0},
    ]
    # Force degenerate zero rates by also zeroing the position prior.
    import app.models.nfl_model as model_module
    original = model_module.POSITION_PRIORS["WR"]["rec_td"]
    model_module.POSITION_PRIORS["WR"]["rec_td"] = 0.0
    try:
        weights = sg.catcher_attribution_weights(catchers)
    finally:
        model_module.POSITION_PRIORS["WR"]["rec_td"] = original
    assert weights.tolist() == pytest.approx([0.5, 0.5])


# ---------- simulate_team_passing_game -----------------------------------------

def test_simulate_team_passing_game_shapes_and_matches_poisson_mean():
    weights = np.array([0.6, 0.3, 0.1])
    team_counts, catcher_counts = sg.simulate_team_passing_game(
        pass_td_lambda=1.6, catcher_weights=weights, iterations=50_000, seed=1,
    )
    assert team_counts.shape == (50_000,)
    assert catcher_counts.shape == (50_000, 3)
    assert team_counts.mean() == pytest.approx(1.6, abs=0.05)
    # Each iteration's per-catcher attribution must sum back to that
    # iteration's team total (the multinomial split is exact, not lossy).
    assert np.array_equal(catcher_counts.sum(axis=1), team_counts)
    # Marginal per-catcher mean should match the Poisson-thinning identity:
    # a multinomial split of a Poisson(lambda) total yields marginal
    # Poisson(lambda * weight_i) counts per category.
    assert catcher_counts[:, 0].mean() == pytest.approx(1.6 * 0.6, abs=0.05)


def test_simulate_team_passing_game_is_deterministic_for_a_fixed_seed():
    weights = np.array([0.5, 0.5])
    a_team, a_catch = sg.simulate_team_passing_game(pass_td_lambda=1.2, catcher_weights=weights, iterations=1000, seed=7)
    b_team, b_catch = sg.simulate_team_passing_game(pass_td_lambda=1.2, catcher_weights=weights, iterations=1000, seed=7)
    assert np.array_equal(a_team, b_team)
    assert np.array_equal(a_catch, b_catch)


def test_receivers_are_negatively_correlated_for_a_fixed_team_total():
    # The defining feature of multinomial attribution: conditional on a
    # fixed team TD count N, one receiver catching more necessarily means
    # less is left for the other — a real correlation naive independence
    # math has no way to express.
    weights = np.array([0.5, 0.5])
    team_counts, catcher_counts = sg.simulate_team_passing_game(
        pass_td_lambda=3.0, catcher_weights=weights, iterations=200_000, seed=3,
    )
    fixed_n_mask = team_counts == 4
    assert fixed_n_mask.sum() > 500  # enough samples at N=4 to measure correlation
    a = catcher_counts[fixed_n_mask, 0]
    b = catcher_counts[fixed_n_mask, 1]
    correlation = np.corrcoef(a, b)[0, 1]
    assert correlation < -0.3


# ---------- build_same_game_pairs_for_team / _for_game --------------------------

def test_build_same_game_pairs_for_team_returns_empty_without_qualifying_qb():
    candidates = [c for c in _synthetic_candidates() if c["market"] != "PassTD"]
    assert sg.build_same_game_pairs_for_team(game_id="401", matchup="SEA @ NE", candidates=candidates) == []


def test_build_same_game_pairs_for_team_returns_empty_without_pass_catchers():
    candidates = [c for c in _synthetic_candidates() if c["market"] == "PassTD"]
    assert sg.build_same_game_pairs_for_team(game_id="401", matchup="SEA @ NE", candidates=candidates) == []


def test_build_same_game_pairs_for_team_shape_and_top_receiver_selection():
    pairs = sg.build_same_game_pairs_for_team(game_id="401", matchup="SEA @ NE", candidates=_synthetic_candidates())
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["game_id"] == "401"
    assert pair["matchup"] == "SEA @ NE"
    assert pair["qb"]["player_name"] == "Test QB"
    # Alpha WR has by far the highest shrunk rec_td rate -> highest
    # attribution weight -> should be the featured receiver.
    assert pair["receiver"]["player_name"] == "Alpha WR"
    assert pair["floor"]["qb_line"] == "1+ Pass TD"
    assert pair["ceiling"]["qb_line"] == "3+ Pass TD"
    assert pair["floor"]["receiver_line"].endswith("+ Rec TD")
    assert pair["ceiling"]["receiver_line"].endswith("+ Rec TD")
    assert 0.0 < pair["floor"]["joint_prob_pct"] <= 100.0
    assert 0.0 < pair["ceiling"]["joint_prob_pct"] <= 100.0
    # Floor should be a substantially more common combo than ceiling.
    assert pair["floor"]["joint_prob_pct"] > pair["ceiling"]["joint_prob_pct"]


def test_joint_probability_genuinely_differs_from_naive_independence_product():
    """The whole point of doing a real sim instead of P(A)*P(B): the QB's
    total and the featured receiver's count share the same underlying
    attribution draw, so they're correlated. This asserts the simulated
    joint deviates meaningfully from the naive product of marginals in both
    directions the model predicts:
      - ceiling: naive product UNDERSTATES the real joint (a big passing
        game raises the receiver's expected share too).
    """
    candidates = _synthetic_candidates()
    qb = sg.extract_qb_ladder(candidates)
    catchers = sg.extract_pass_catchers(candidates, exclude_player_id=qb["player_id"])
    weights = sg.catcher_attribution_weights(catchers)
    top_weight = float(weights.max())
    lam = qb["pass_td_lambda"]

    pairs = sg.build_same_game_pairs_for_team(game_id="401", matchup="SEA @ NE", candidates=candidates)
    pair = pairs[0]

    for tier, rung_key in (("floor", "floor"), ("ceiling", "ceiling")):
        rung = pair[rung_key]
        qb_k = int(rung["qb_line"].split("+")[0])
        receiver_j = int(rung["receiver_line"].split("+")[0])
        # Naive independence: marginal QB tail probability times the
        # receiver's OWN marginal tail probability. By the Poisson-thinning
        # identity the receiver's true unconditional marginal is exactly
        # Poisson(lambda * weight), so this is the strongest fair naive
        # baseline (not a strawman) -- and the sim's joint still deviates
        # from it because the two events are NOT independent.
        naive_product = nfl_model.poisson_at_least(lam, qb_k) * nfl_model.poisson_at_least(lam * top_weight, receiver_j)
        sim_joint = rung["joint_prob_pct"] / 100.0
        assert sim_joint != pytest.approx(naive_product, abs=0.005), (
            f"{tier}: sim joint {sim_joint} should not equal the naive independent product {naive_product}"
        )
        # Positive correlation: a bigger passing game raises the featured
        # receiver's own count too, so the real joint should exceed the
        # naive (independence-assumed) product.
        assert sim_joint > naive_product


# ---------- build_same_game_pairs_for_game (multi-team) -------------------------

def test_build_same_game_pairs_for_game_covers_both_teams():
    home_candidates = _synthetic_candidates()  # team "SEA"
    away_candidates = [dict(c, team="NE", player_id=f"ne_{c['player_id']}") for c in _synthetic_candidates()]
    pairs = sg.build_same_game_pairs_for_game(
        game_id="401", matchup="SEA @ NE", candidates=home_candidates + away_candidates,
    )
    teams_represented = set()
    for pair in pairs:
        if pair["qb"]["player_id"] == "qb1":
            teams_represented.add("SEA")
        elif pair["qb"]["player_id"] == "ne_qb1":
            teams_represented.add("NE")
    assert teams_represented == {"SEA", "NE"}


def test_build_same_game_pairs_for_game_empty_when_no_candidates():
    assert sg.build_same_game_pairs_for_game(game_id="401", matchup="SEA @ NE", candidates=[]) == []
