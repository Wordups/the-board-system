"""Pure-math tests for the NFL same-PLAYER RB stat-stack correlation sim
(Gaussian-copula blend of RushYds/RecYds/TD) against fixed synthetic
candidate lists -- no live network calls, mirrors test_nfl_qb_stack.py's
style and rigor for its sibling module.

The flagship test (`test_joint_probability_genuinely_differs_from_naive_
independence_product`) is the point of this module: it proves the simulated
joint probability is genuinely different from naive P(A)*P(B)*P(C)
independence math, which is the whole reason this is a real copula sim
rather than three multiplied marginals.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import numpy as np
import pytest

from app.models import nfl_model
from app.sim import nfl_rb_stack as srs


def _synthetic_candidates():
    """One RB with posted RushYds/RecYds/TD rows, shaped like the tagged
    rows build_team_player_candidates() now produces (rush_yds_mean/
    rec_yds_mean/td_lambda tagged on every RB row, per nfl_collector.py).
    Line strings match what nfl_model.yardage_line() actually produces at
    the collector's real default floor share (0.82) so this fixture isn't
    inventing numbers the real pipeline wouldn't post: RushYds mean 90.0 ->
    round(90*0.82/5)*5 = 75; RecYds mean 35.0 -> round(35*0.82/5)*5 = 30."""
    common = {"team": "SEA", "player_id": "rb1", "player_name": "Test RB",
              "position": "RB", "games_played": 17.0,
              "rush_yds_mean": 90.0, "rec_yds_mean": 35.0, "td_lambda": 0.9}
    return [
        {**common, "market": "RushYds", "line": "75+ Rush Yds"},
        {**common, "market": "RecYds", "line": "30+ Rec Yds"},
        {**common, "market": "TD", "line": "Anytime TD"},
    ]


# ---------- extract_rb_stat_stack ---------------------------------------------

def test_extract_rb_stat_stack_reads_floor_material():
    stack = srs.extract_rb_stat_stack(_synthetic_candidates())
    assert stack is not None
    assert stack["player_id"] == "rb1"
    assert stack["rush_yds_mean"] == pytest.approx(90.0)
    assert stack["floor_rush_yds_line"] == 75
    assert stack["rec_yds_mean"] == pytest.approx(35.0)
    assert stack["floor_rec_yds_line"] == 30
    assert stack["td_lambda"] == pytest.approx(0.9)


def test_extract_rb_stat_stack_returns_none_without_rush_yds_row():
    candidates = [c for c in _synthetic_candidates() if c["market"] != "RushYds"]
    assert srs.extract_rb_stat_stack(candidates) is None


def test_extract_rb_stat_stack_returns_none_without_rec_yds_row():
    candidates = [c for c in _synthetic_candidates() if c["market"] != "RecYds"]
    assert srs.extract_rb_stat_stack(candidates) is None


def test_extract_rb_stat_stack_returns_none_without_td_row():
    candidates = [c for c in _synthetic_candidates() if c["market"] != "TD"]
    assert srs.extract_rb_stat_stack(candidates) is None


def test_extract_rb_stat_stack_ignores_non_rb_rows():
    candidates = _synthetic_candidates() + [
        {"team": "SEA", "player_id": "wr1", "player_name": "Test WR", "position": "WR",
         "market": "RecYds", "line": "60+ Rec Yds", "rec_yds_mean": 70.0}
    ]
    stack = srs.extract_rb_stat_stack(candidates)
    assert stack is not None
    assert stack["player_id"] == "rb1"


def test_extract_rb_stat_stack_picks_higher_combined_usage_back_when_two_qualify():
    rb1 = _synthetic_candidates()  # rush 90 + rec 35 = 125
    rb2 = [
        {**c, "player_id": "rb2", "player_name": "Backup RB",
         "rush_yds_mean": 20.0, "rec_yds_mean": 10.0, "td_lambda": 0.2,
         "line": {"RushYds": "15+ Rush Yds", "RecYds": "8+ Rec Yds", "TD": "Anytime TD"}[c["market"]]}
        for c in _synthetic_candidates()
    ]  # rush 20 + rec 10 = 30
    stack = srs.extract_rb_stat_stack(rb1 + rb2)
    assert stack["player_id"] == "rb1"


# ---------- simulate_rb_stat_stack ---------------------------------------------

def test_simulate_rb_stat_stack_shapes():
    rush_samples, rec_samples, td_samples = srs.simulate_rb_stat_stack(
        rush_yds_mean=90.0, rush_yds_std=49.5,
        rec_yds_mean=35.0, rec_yds_std=19.25,
        td_lambda=0.9, iterations=20_000, seed=1,
    )
    assert rush_samples.shape == (20_000,)
    assert rec_samples.shape == (20_000,)
    assert td_samples.shape == (20_000,)
    assert td_samples.dtype == np.int64
    assert (td_samples >= 0).all()


def test_simulate_rb_stat_stack_is_deterministic_for_a_fixed_seed():
    kwargs = dict(rush_yds_mean=90.0, rush_yds_std=49.5, rec_yds_mean=35.0,
                  rec_yds_std=19.25, td_lambda=0.9, iterations=5_000, seed=7)
    a_rush, a_rec, a_td = srs.simulate_rb_stat_stack(**kwargs)
    b_rush, b_rec, b_td = srs.simulate_rb_stat_stack(**kwargs)
    assert np.array_equal(a_rush, b_rush)
    assert np.array_equal(a_rec, b_rec)
    assert np.array_equal(a_td, b_td)


def test_simulate_rb_stat_stack_marginals_match_each_markets_own_calibration():
    """The copula blend must not distort any single market's own
    already-calibrated marginal distribution -- only the correlation
    BETWEEN markets is new here. Checked against the exact same
    nfl_model.normal_at_least / poisson_at_least calls the collector uses
    to score these markets individually (RushYds/RecYds use normal_at_least
    with NO std_ratio/min_std override in the collector, i.e. its own
    defaults -- see srs.RB_YDS_STD_RATIO / RB_YDS_MIN_STD)."""
    rush_yds_mean, rec_yds_mean, lam = 90.0, 35.0, 0.9
    rush_yds_std = max(srs.RB_YDS_MIN_STD, rush_yds_mean * srs.RB_YDS_STD_RATIO)
    rec_yds_std = max(srs.RB_YDS_MIN_STD, rec_yds_mean * srs.RB_YDS_STD_RATIO)

    rush_samples, rec_samples, td_samples = srs.simulate_rb_stat_stack(
        rush_yds_mean=rush_yds_mean, rush_yds_std=rush_yds_std,
        rec_yds_mean=rec_yds_mean, rec_yds_std=rec_yds_std,
        td_lambda=lam, rho=srs.RHO, iterations=200_000, seed=5,
    )
    n = len(rush_samples)

    for line in (60, 75, 100):
        expected = nfl_model.normal_at_least(rush_yds_mean, line)
        simulated = float(np.count_nonzero(rush_samples >= line)) / n
        assert simulated == pytest.approx(expected, abs=0.01)

    for line in (20, 30, 45):
        expected = nfl_model.normal_at_least(rec_yds_mean, line)
        simulated = float(np.count_nonzero(rec_samples >= line)) / n
        assert simulated == pytest.approx(expected, abs=0.01)

    for k in (1, 2):
        expected = nfl_model.poisson_at_least(lam, k)
        simulated = float(np.count_nonzero(td_samples >= k)) / n
        assert simulated == pytest.approx(expected, abs=0.01)


def test_higher_rho_widens_gap_vs_naive_independence():
    """RHO=0 must collapse the sim to (near-)independence -- with z_shared's
    weight sqrt(rho)=0, each market's blended z is exactly its own
    idiosyncratic draw, no shared factor at all. The real RHO should then
    produce a meaningfully wider sim-vs-naive gap than that baseline."""
    candidates = _synthetic_candidates()
    stack = srs.extract_rb_stat_stack(candidates)
    rush_yds_std = max(srs.RB_YDS_MIN_STD, stack["rush_yds_mean"] * srs.RB_YDS_STD_RATIO)
    rec_yds_std = max(srs.RB_YDS_MIN_STD, stack["rec_yds_mean"] * srs.RB_YDS_STD_RATIO)

    naive = (
        nfl_model.normal_at_least(stack["rush_yds_mean"], stack["floor_rush_yds_line"])
        * nfl_model.normal_at_least(stack["rec_yds_mean"], stack["floor_rec_yds_line"])
        * nfl_model.poisson_at_least(stack["td_lambda"], 1)
    )

    def joint_at(rho: float) -> float:
        rush_samples, rec_samples, td_samples = srs.simulate_rb_stat_stack(
            rush_yds_mean=stack["rush_yds_mean"], rush_yds_std=rush_yds_std,
            rec_yds_mean=stack["rec_yds_mean"], rec_yds_std=rec_yds_std,
            td_lambda=stack["td_lambda"], rho=rho, iterations=150_000, seed=11,
        )
        return srs._joint_prob(
            rush_samples, rec_samples, td_samples,
            rush_yds_line=stack["floor_rush_yds_line"],
            rec_yds_line=stack["floor_rec_yds_line"],
            td_rung=1,
        )

    gap_at_zero = joint_at(0.0) - naive
    gap_at_real_rho = joint_at(srs.RHO) - naive
    assert abs(gap_at_zero) < 0.015, f"RHO=0 should collapse to ~independence, got gap {gap_at_zero}"
    assert gap_at_real_rho > gap_at_zero + 0.02, (
        f"real RHO ({srs.RHO}) should meaningfully widen the sim-vs-naive gap "
        f"vs RHO=0 (got {gap_at_zero} -> {gap_at_real_rho})"
    )


# ---------- build_rb_stack_for_team / _for_game ---------------------------------

def test_build_rb_stack_for_team_returns_empty_without_qualifying_rb():
    candidates = [c for c in _synthetic_candidates() if c["market"] != "TD"]
    assert srs.build_rb_stack_for_team(game_id="401", matchup="SEA @ NE", candidates=candidates) == []


def test_build_rb_stack_for_team_shape_and_floor_ceiling_ordering():
    stacks = srs.build_rb_stack_for_team(game_id="401", matchup="SEA @ NE", candidates=_synthetic_candidates())
    assert len(stacks) == 1
    stack = stacks[0]
    assert stack["game_id"] == "401"
    assert stack["matchup"] == "SEA @ NE"
    assert stack["rb"]["player_name"] == "Test RB"

    floor, ceiling = stack["floor"], stack["ceiling"]
    for field in ("rush_yds_line", "rec_yds_line", "td_line"):
        assert floor[field].split("+")[0].isdigit()
        assert ceiling[field].split("+")[0].isdigit()

    floor_rush = int(floor["rush_yds_line"].split("+")[0])
    ceiling_rush = int(ceiling["rush_yds_line"].split("+")[0])
    floor_rec = int(floor["rec_yds_line"].split("+")[0])
    ceiling_rec = int(ceiling["rec_yds_line"].split("+")[0])
    floor_td = int(floor["td_line"].split("+")[0])
    ceiling_td = int(ceiling["td_line"].split("+")[0])
    # Floor lines/rung must never exceed ceiling lines/rung.
    assert floor_rush <= ceiling_rush
    assert floor_rec <= ceiling_rec
    assert floor_td <= ceiling_td

    assert 0.0 < floor["joint_prob_pct"] <= 100.0
    assert 0.0 < ceiling["joint_prob_pct"] <= 100.0
    # A three-market floor stack should be a substantially more common combo
    # than the three-market ceiling stack.
    assert floor["joint_prob_pct"] > ceiling["joint_prob_pct"]


def test_joint_probability_genuinely_differs_from_naive_independence_product():
    """The whole point of doing a real copula sim instead of
    P(A)*P(B)*P(C): all three markets share the same underlying z_shared
    draw, so they're positively correlated. This asserts the simulated
    joint deviates meaningfully from the naive product of each market's own
    marginal probability (the strongest fair baseline -- the exact
    nfl_model calls that score these markets individually, not a
    strawman), and does so in the direction the model predicts: a
    positively-correlated joint upper-tail probability exceeds the
    independent product, for both the floor and ceiling stacks."""
    candidates = _synthetic_candidates()
    stacks = srs.build_rb_stack_for_team(game_id="401", matchup="SEA @ NE", candidates=candidates)
    stack = stacks[0]
    material = srs.extract_rb_stat_stack(candidates)

    for tier in ("floor", "ceiling"):
        rung = stack[tier]
        rush_k = int(rung["rush_yds_line"].split("+")[0])
        rec_k = int(rung["rec_yds_line"].split("+")[0])
        td_k = int(rung["td_line"].split("+")[0])

        naive_product = (
            nfl_model.normal_at_least(material["rush_yds_mean"], rush_k)
            * nfl_model.normal_at_least(material["rec_yds_mean"], rec_k)
            * nfl_model.poisson_at_least(material["td_lambda"], td_k)
        )
        sim_joint = rung["joint_prob_pct"] / 100.0
        assert sim_joint != pytest.approx(naive_product, abs=0.005), (
            f"{tier}: sim joint {sim_joint} should not equal the naive independent product {naive_product}"
        )
        assert sim_joint > naive_product, (
            f"{tier}: positively-correlated joint ({sim_joint}) should exceed the naive "
            f"independent product ({naive_product})"
        )


def test_build_rb_stacks_for_game_covers_both_teams():
    home_candidates = _synthetic_candidates()  # team "SEA"
    away_candidates = [dict(c, team="NE", player_id="ne_rb1") for c in _synthetic_candidates()]
    stacks = srs.build_rb_stacks_for_game(
        game_id="401", matchup="SEA @ NE", candidates=home_candidates + away_candidates,
    )
    teams_represented = {stack["rb"]["player_id"] for stack in stacks}
    assert teams_represented == {"rb1", "ne_rb1"}


def test_build_rb_stacks_for_game_empty_when_no_candidates():
    assert srs.build_rb_stacks_for_game(game_id="401", matchup="SEA @ NE", candidates=[]) == []
