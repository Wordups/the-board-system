"""Pure-math tests for the NFL same-PLAYER QB stat-stack correlation sim
(Gaussian-copula blend of PassYds/Completions/PassTD) against fixed
synthetic candidate lists — no live network calls, mirrors
test_nfl_same_game.py's style and rigor for its sibling module.

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
from app.sim import nfl_qb_stack as sqs


def _synthetic_candidates():
    """One QB with a PassTD ladder (1+/2+/3+, lambda 1.6) and posted
    PassYds/Completions rows, shaped like the tagged rows
    build_team_player_candidates() now produces (pass_yds_mean/
    completions_mean tagged on every QB row, per nfl_collector.py).
    Line strings match what nfl_model.yardage_line() actually produces at
    the collector's real floor shares (215 = round(260*0.82/5)*5, 20 =
    round(24*0.85)) so this fixture isn't inventing numbers the real
    pipeline wouldn't post."""
    common = {"team": "SEA", "player_id": "qb1", "player_name": "Test QB",
              "position": "QB", "games_played": 17.0, "rec_td_per_game": 0.0,
              "pass_yds_mean": 260.0, "completions_mean": 24.0}
    return [
        {**common, "market": "PassTD", "line": "1+ Pass TD", "pass_td_lambda": 1.6},
        {**common, "market": "PassTD", "line": "2+ Pass TD", "pass_td_lambda": 1.6},
        {**common, "market": "PassTD", "line": "3+ Pass TD", "pass_td_lambda": 1.6},
        {**common, "market": "PassYds", "line": "215+ Pass Yds"},
        {**common, "market": "Completions", "line": "20+ Completions"},
    ]


# ---------- extract_qb_stat_stack ---------------------------------------------

def test_extract_qb_stat_stack_reads_floor_material():
    stack = sqs.extract_qb_stat_stack(_synthetic_candidates())
    assert stack is not None
    assert stack["player_id"] == "qb1"
    assert stack["pass_td_lambda"] == pytest.approx(1.6)
    assert stack["floor_pass_td_rung"] == 1
    assert stack["ceiling_pass_td_rung"] == 3
    assert stack["pass_yds_mean"] == pytest.approx(260.0)
    assert stack["floor_pass_yds_line"] == 215
    assert stack["completions_mean"] == pytest.approx(24.0)
    assert stack["floor_completions_line"] == 20


def test_extract_qb_stat_stack_returns_none_without_pass_td_ladder():
    candidates = [c for c in _synthetic_candidates() if c["market"] != "PassTD"]
    assert sqs.extract_qb_stat_stack(candidates) is None


def test_extract_qb_stat_stack_returns_none_without_pass_yds_row():
    candidates = [c for c in _synthetic_candidates() if c["market"] != "PassYds"]
    assert sqs.extract_qb_stat_stack(candidates) is None


def test_extract_qb_stat_stack_returns_none_without_completions_row():
    candidates = [c for c in _synthetic_candidates() if c["market"] != "Completions"]
    assert sqs.extract_qb_stat_stack(candidates) is None


# ---------- simulate_qb_stat_stack ---------------------------------------------

def test_simulate_qb_stat_stack_shapes():
    pass_yds_samples, completions_samples, pass_td_samples = sqs.simulate_qb_stat_stack(
        pass_yds_mean=260.0, pass_yds_std=85.8,
        completions_mean=24.0, completions_std=5.28,
        pass_td_lambda=1.6, iterations=20_000, seed=1,
    )
    assert pass_yds_samples.shape == (20_000,)
    assert completions_samples.shape == (20_000,)
    assert pass_td_samples.shape == (20_000,)
    assert pass_td_samples.dtype == np.int64
    assert (pass_td_samples >= 0).all()


def test_simulate_qb_stat_stack_is_deterministic_for_a_fixed_seed():
    kwargs = dict(pass_yds_mean=260.0, pass_yds_std=85.8, completions_mean=24.0,
                  completions_std=5.28, pass_td_lambda=1.6, iterations=5_000, seed=7)
    a_yds, a_comp, a_td = sqs.simulate_qb_stat_stack(**kwargs)
    b_yds, b_comp, b_td = sqs.simulate_qb_stat_stack(**kwargs)
    assert np.array_equal(a_yds, b_yds)
    assert np.array_equal(a_comp, b_comp)
    assert np.array_equal(a_td, b_td)


def test_simulate_qb_stat_stack_marginals_match_each_markets_own_calibration():
    """The copula blend must not distort any single market's own
    already-calibrated marginal distribution -- only the correlation
    BETWEEN markets is new here. Checked against the exact same
    nfl_model.normal_at_least / poisson_at_least calls the collector uses
    to score these markets individually."""
    pass_yds_mean, completions_mean, lam = 260.0, 24.0, 1.6
    pass_yds_std = max(sqs.PASS_YDS_MIN_STD, pass_yds_mean * sqs.PASS_YDS_STD_RATIO)
    completions_std = max(sqs.COMPLETIONS_MIN_STD, completions_mean * sqs.COMPLETIONS_STD_RATIO)

    pass_yds_samples, completions_samples, pass_td_samples = sqs.simulate_qb_stat_stack(
        pass_yds_mean=pass_yds_mean, pass_yds_std=pass_yds_std,
        completions_mean=completions_mean, completions_std=completions_std,
        pass_td_lambda=lam, rho=sqs.RHO, iterations=200_000, seed=5,
    )
    n = len(pass_yds_samples)

    for line in (215, 260, 300):
        expected = nfl_model.normal_at_least(
            pass_yds_mean, line, std_ratio=sqs.PASS_YDS_STD_RATIO, min_std=sqs.PASS_YDS_MIN_STD
        )
        simulated = float(np.count_nonzero(pass_yds_samples >= line)) / n
        assert simulated == pytest.approx(expected, abs=0.01)

    for line in (18, 24, 30):
        expected = nfl_model.normal_at_least(
            completions_mean, line, std_ratio=sqs.COMPLETIONS_STD_RATIO, min_std=sqs.COMPLETIONS_MIN_STD
        )
        simulated = float(np.count_nonzero(completions_samples >= line)) / n
        assert simulated == pytest.approx(expected, abs=0.01)

    for k in (1, 2, 3):
        expected = nfl_model.poisson_at_least(lam, k)
        simulated = float(np.count_nonzero(pass_td_samples >= k)) / n
        assert simulated == pytest.approx(expected, abs=0.01)


def test_higher_rho_widens_gap_vs_naive_independence():
    """RHO=0 must collapse the sim to (near-)independence -- with z_shared's
    weight sqrt(rho)=0, each market's blended z is exactly its own
    idiosyncratic draw, no shared factor at all. The real RHO should then
    produce a meaningfully wider sim-vs-naive gap than that baseline."""
    candidates = _synthetic_candidates()
    stack = sqs.extract_qb_stat_stack(candidates)
    pass_yds_std = max(sqs.PASS_YDS_MIN_STD, stack["pass_yds_mean"] * sqs.PASS_YDS_STD_RATIO)
    completions_std = max(sqs.COMPLETIONS_MIN_STD, stack["completions_mean"] * sqs.COMPLETIONS_STD_RATIO)

    naive = (
        nfl_model.normal_at_least(stack["pass_yds_mean"], stack["floor_pass_yds_line"], std_ratio=sqs.PASS_YDS_STD_RATIO, min_std=sqs.PASS_YDS_MIN_STD)
        * nfl_model.normal_at_least(stack["completions_mean"], stack["floor_completions_line"], std_ratio=sqs.COMPLETIONS_STD_RATIO, min_std=sqs.COMPLETIONS_MIN_STD)
        * nfl_model.poisson_at_least(stack["pass_td_lambda"], stack["floor_pass_td_rung"])
    )

    def joint_at(rho: float) -> float:
        pass_yds_samples, completions_samples, pass_td_samples = sqs.simulate_qb_stat_stack(
            pass_yds_mean=stack["pass_yds_mean"], pass_yds_std=pass_yds_std,
            completions_mean=stack["completions_mean"], completions_std=completions_std,
            pass_td_lambda=stack["pass_td_lambda"], rho=rho, iterations=150_000, seed=11,
        )
        return sqs._joint_prob(
            pass_yds_samples, completions_samples, pass_td_samples,
            pass_yds_line=stack["floor_pass_yds_line"],
            completions_line=stack["floor_completions_line"],
            pass_td_rung=stack["floor_pass_td_rung"],
        )

    gap_at_zero = joint_at(0.0) - naive
    gap_at_real_rho = joint_at(sqs.RHO) - naive
    assert abs(gap_at_zero) < 0.015, f"RHO=0 should collapse to ~independence, got gap {gap_at_zero}"
    assert gap_at_real_rho > gap_at_zero + 0.02, (
        f"real RHO ({sqs.RHO}) should meaningfully widen the sim-vs-naive gap "
        f"vs RHO=0 (got {gap_at_zero} -> {gap_at_real_rho})"
    )


# ---------- build_qb_stack_for_team / _for_game ---------------------------------

def test_build_qb_stack_for_team_returns_empty_without_qualifying_qb():
    candidates = [c for c in _synthetic_candidates() if c["market"] != "PassTD"]
    assert sqs.build_qb_stack_for_team(game_id="401", matchup="SEA @ NE", candidates=candidates) == []


def test_build_qb_stack_for_team_shape_and_floor_ceiling_ordering():
    stacks = sqs.build_qb_stack_for_team(game_id="401", matchup="SEA @ NE", candidates=_synthetic_candidates())
    assert len(stacks) == 1
    stack = stacks[0]
    assert stack["game_id"] == "401"
    assert stack["matchup"] == "SEA @ NE"
    assert stack["qb"]["player_name"] == "Test QB"

    floor, ceiling = stack["floor"], stack["ceiling"]
    for field in ("pass_yds_line", "completions_line", "pass_td_line"):
        assert floor[field].split("+")[0].isdigit()
        assert ceiling[field].split("+")[0].isdigit()

    floor_yds = int(floor["pass_yds_line"].split("+")[0])
    ceiling_yds = int(ceiling["pass_yds_line"].split("+")[0])
    floor_comp = int(floor["completions_line"].split("+")[0])
    ceiling_comp = int(ceiling["completions_line"].split("+")[0])
    floor_td = int(floor["pass_td_line"].split("+")[0])
    ceiling_td = int(ceiling["pass_td_line"].split("+")[0])
    # Floor rung/lines must never exceed ceiling rung/lines.
    assert floor_yds <= ceiling_yds
    assert floor_comp <= ceiling_comp
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
    stacks = sqs.build_qb_stack_for_team(game_id="401", matchup="SEA @ NE", candidates=candidates)
    stack = stacks[0]
    material = sqs.extract_qb_stat_stack(candidates)

    for tier in ("floor", "ceiling"):
        rung = stack[tier]
        yds_k = int(rung["pass_yds_line"].split("+")[0])
        comp_k = int(rung["completions_line"].split("+")[0])
        td_k = int(rung["pass_td_line"].split("+")[0])

        naive_product = (
            nfl_model.normal_at_least(material["pass_yds_mean"], yds_k, std_ratio=sqs.PASS_YDS_STD_RATIO, min_std=sqs.PASS_YDS_MIN_STD)
            * nfl_model.normal_at_least(material["completions_mean"], comp_k, std_ratio=sqs.COMPLETIONS_STD_RATIO, min_std=sqs.COMPLETIONS_MIN_STD)
            * nfl_model.poisson_at_least(material["pass_td_lambda"], td_k)
        )
        sim_joint = rung["joint_prob_pct"] / 100.0
        assert sim_joint != pytest.approx(naive_product, abs=0.005), (
            f"{tier}: sim joint {sim_joint} should not equal the naive independent product {naive_product}"
        )
        assert sim_joint > naive_product, (
            f"{tier}: positively-correlated joint ({sim_joint}) should exceed the naive "
            f"independent product ({naive_product})"
        )


def test_build_qb_stacks_for_game_covers_both_teams():
    home_candidates = _synthetic_candidates()  # team "SEA"
    away_candidates = [dict(c, team="NE", player_id="ne_qb1") for c in _synthetic_candidates()]
    stacks = sqs.build_qb_stacks_for_game(
        game_id="401", matchup="SEA @ NE", candidates=home_candidates + away_candidates,
    )
    teams_represented = {stack["qb"]["player_id"] for stack in stacks}
    assert teams_represented == {"qb1", "ne_qb1"}


def test_build_qb_stacks_for_game_empty_when_no_candidates():
    assert sqs.build_qb_stacks_for_game(game_id="401", matchup="SEA @ NE", candidates=[]) == []
