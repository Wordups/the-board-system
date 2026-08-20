"""Pure-math tests for the NFL model (shrinkage / matchup-adjustment / odds
math) on fixed synthetic inputs — no live network calls, mirrors
test_prob_shrinkage.py's style for backend/app/scoring/prob_shrinkage.py."""

from __future__ import annotations

import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import pytest

from app.models import nfl_model


# ---------- shrunk_rate ------------------------------------------------------

def test_shrunk_rate_returns_pure_prior_with_zero_sample():
    assert nfl_model.shrunk_rate(0.0, prior=0.30, sample_games=0.0) == pytest.approx(0.30)


def test_shrunk_rate_blends_toward_prior_for_small_sample():
    # 1 game observed at 1.0, prior 0.30, prior weight 4 -> (1*1 + 0.30*4) / 5
    value = nfl_model.shrunk_rate(1.0, prior=0.30, sample_games=1.0, prior_weight=4.0)
    assert value == pytest.approx((1.0 * 1.0 + 0.30 * 4.0) / 5.0)


def test_shrunk_rate_dominated_by_large_sample():
    # 17 games at 0.50 vs a 0.10 prior weighted at 4 games — observed rate dominates.
    value = nfl_model.shrunk_rate(0.50, prior=0.10, sample_games=17.0, prior_weight=4.0)
    assert value > 0.40
    assert value < 0.50


# ---------- matchup_ratio ----------------------------------------------------

def test_matchup_ratio_average_matchup_is_one():
    assert nfl_model.matchup_ratio(120.0, 120.0) == pytest.approx(1.0)


def test_matchup_ratio_clips_to_ceiling():
    assert nfl_model.matchup_ratio(1000.0, 100.0) == pytest.approx(nfl_model.MATCHUP_CEILING)


def test_matchup_ratio_clips_to_floor():
    assert nfl_model.matchup_ratio(1.0, 100.0) == pytest.approx(nfl_model.MATCHUP_FLOOR)


def test_matchup_ratio_zero_baseline_is_neutral():
    assert nfl_model.matchup_ratio(50.0, 0.0) == 1.0


# ---------- poisson_at_least -------------------------------------------------

def test_poisson_at_least_zero_threshold_is_certain():
    assert nfl_model.poisson_at_least(0.4, 0) == 1.0


def test_poisson_at_least_matches_hand_computed_value():
    # P(X >= 1) for Poisson(0.5) = 1 - e^-0.5
    expected = 1.0 - math.exp(-0.5)
    assert nfl_model.poisson_at_least(0.5, 1) == pytest.approx(expected, abs=1e-9)


def test_poisson_at_least_increases_with_rate():
    low = nfl_model.poisson_at_least(0.2, 1)
    high = nfl_model.poisson_at_least(0.8, 1)
    assert high > low


# ---------- normal_at_least ---------------------------------------------------

def test_normal_at_least_half_at_the_mean():
    assert nfl_model.normal_at_least(50.0, 50.0) == pytest.approx(0.5, abs=1e-9)


def test_normal_at_least_below_half_above_the_mean():
    assert nfl_model.normal_at_least(50.0, 60.0) < 0.5


def test_normal_at_least_above_half_below_the_mean():
    assert nfl_model.normal_at_least(50.0, 40.0) > 0.5


def test_normal_at_least_floors_std_for_low_mean():
    # Two very low means should not produce near-zero std / a degenerate step function.
    p1 = nfl_model.normal_at_least(2.0, 5.0)
    assert 0.01 < p1 < 0.5


# ---------- yardage_line ------------------------------------------------------

def test_yardage_line_is_below_the_mean_and_rounded():
    line = nfl_model.yardage_line(83.0, round_to=5)
    assert line < 83.0
    assert line % 5 == 0


def test_yardage_line_respects_minimum():
    line = nfl_model.yardage_line(1.0, minimum=5)
    assert line >= 5


# ---------- moneyline / team power -------------------------------------------

def test_moneyline_symmetric_without_home_edge():
    prob = nfl_model.moneyline_win_probability(10.0, 10.0, home_edge=0.0)
    assert prob == pytest.approx(0.5, abs=1e-9)


def test_moneyline_favors_the_higher_power_team():
    favorite = nfl_model.moneyline_win_probability(10.0, -5.0, home_edge=0.0)
    underdog = nfl_model.moneyline_win_probability(-5.0, 10.0, home_edge=0.0)
    assert favorite > 0.5
    assert underdog < 0.5
    assert favorite == pytest.approx(1.0 - underdog, abs=1e-9)


def test_moneyline_home_edge_helps_the_home_team():
    even = nfl_model.moneyline_win_probability(0.0, 0.0, home_edge=0.0)
    with_edge = nfl_model.moneyline_win_probability(0.0, 0.0, home_edge=3.0)
    assert with_edge > even


def test_team_power_rating_rewards_positive_point_differential():
    strong = nfl_model.team_power_rating(point_diff_per_game=10.0, recent_point_diff_per_game=10.0, win_pct=0.7)
    weak = nfl_model.team_power_rating(point_diff_per_game=-10.0, recent_point_diff_per_game=-10.0, win_pct=0.3)
    assert strong > weak


# ---------- projection helpers ------------------------------------------------

def test_project_td_lambda_falls_back_to_position_prior_with_no_sample():
    lam = nfl_model.project_td_lambda(
        rush_td_per_game=0.0, rec_td_per_game=0.0, sample_games=0.0, position="RB",
        rush_matchup=1.0, rec_matchup=1.0,
    )
    prior = nfl_model.POSITION_PRIORS["RB"]
    assert lam == pytest.approx(prior["rush_td"] + prior["rec_td"], abs=1e-6)


def test_project_td_lambda_scales_with_matchup():
    baseline = nfl_model.project_td_lambda(
        rush_td_per_game=0.5, rec_td_per_game=0.1, sample_games=17.0, position="RB",
        rush_matchup=1.0, rec_matchup=1.0,
    )
    softer_matchup = nfl_model.project_td_lambda(
        rush_td_per_game=0.5, rec_td_per_game=0.1, sample_games=17.0, position="RB",
        rush_matchup=1.3, rec_matchup=1.3,
    )
    assert softer_matchup > baseline


def test_project_pass_td_lambda_uses_qb_prior_with_no_sample():
    lam = nfl_model.project_pass_td_lambda(pass_td_per_game=0.0, sample_games=0.0, pass_matchup=1.0)
    assert lam == pytest.approx(nfl_model.POSITION_PRIORS["QB"]["pass_td"], abs=1e-6)


def test_project_pass_yds_mean_uses_qb_prior_with_no_sample():
    mean = nfl_model.project_pass_yds_mean(pass_yds_per_game=0.0, sample_games=0.0, pass_matchup=1.0)
    assert mean == pytest.approx(nfl_model.POSITION_PRIORS["QB"]["pass_yds"], abs=1e-6)


def test_project_pass_yds_mean_full_season_dominates_prior():
    # A real 17-game 2025 sample should pull the mean toward the observed
    # rate, not stay anchored near the position prior.
    mean = nfl_model.project_pass_yds_mean(pass_yds_per_game=310.0, sample_games=17.0, pass_matchup=1.0)
    assert mean > 280.0


def test_project_pass_yds_mean_applies_matchup_multiplier():
    baseline = nfl_model.project_pass_yds_mean(pass_yds_per_game=250.0, sample_games=17.0, pass_matchup=1.0)
    softer_matchup = nfl_model.project_pass_yds_mean(pass_yds_per_game=250.0, sample_games=17.0, pass_matchup=1.2)
    assert softer_matchup > baseline


def test_project_completions_mean_uses_qb_prior_with_no_sample():
    mean = nfl_model.project_completions_mean(completions_per_game=0.0, sample_games=0.0, pass_matchup=1.0)
    assert mean == pytest.approx(nfl_model.POSITION_PRIORS["QB"]["completions"], abs=1e-6)


def test_project_completions_mean_applies_matchup_multiplier():
    baseline = nfl_model.project_completions_mean(completions_per_game=24.0, sample_games=17.0, pass_matchup=1.0)
    softer_matchup = nfl_model.project_completions_mean(completions_per_game=24.0, sample_games=17.0, pass_matchup=1.15)
    assert softer_matchup > baseline


def test_project_interceptions_lambda_uses_qb_prior_with_no_sample():
    lam = nfl_model.project_interceptions_lambda(interceptions_per_game=0.0, sample_games=0.0)
    assert lam == pytest.approx(nfl_model.POSITION_PRIORS["QB"]["interceptions"], abs=1e-6)


def test_project_interceptions_lambda_full_season_dominates_prior():
    lam = nfl_model.project_interceptions_lambda(interceptions_per_game=1.8, sample_games=17.0)
    assert lam > 1.4


def test_project_interceptions_lambda_has_no_matchup_parameter():
    # Deliberate: no takeaway-rate data is collected, so this must not
    # silently accept/ignore a matchup kwarg that would imply it does.
    import inspect
    params = inspect.signature(nfl_model.project_interceptions_lambda).parameters
    assert "pass_matchup" not in params and "matchup" not in params


def test_clamp_probability_bounds():
    assert nfl_model.clamp_probability(5.0) == 0.99
    assert nfl_model.clamp_probability(-5.0) == 0.01
    assert nfl_model.clamp_probability(0.5) == 0.5
