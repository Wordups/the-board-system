"""NFL player-market probability model.

Pure math, no ESPN I/O: turns a player's shrunk per-game rate (blended with a
position prior so a rookie's 4-game September sample doesn't swing wildly)
and an opponent matchup multiplier into a market probability. Mirrors the
collector -> model -> scoring/tiers -> builder shape every other sport in
this repo uses, and follows soccer_collector.py's inline Poisson-rate
approach (shrunk_rate + poisson_at_least) for count stats (TD, REC, PassTD).

Rushing/receiving YARDS are NOT modeled as Poisson here — a Poisson(50) has
stdev ~7, which is far tighter than real game-to-game NFL yardage variance.
Yardage markets use a Normal approximation instead (normal_at_least), with
std scaled to the mean rather than fixed.

Kept import-free of app.collectors.* and app.builders.* so it's independently
unit-testable against fixed synthetic inputs (no live network calls).
"""

from __future__ import annotations

import math
from typing import Any


# ---------------------------------------------------------------------------
# Position priors: rough NFL per-game rate anchors used ONLY as Bayesian
# shrink targets for small samples. Not meant to be standalone projections —
# a real player's own shrunk rate dominates once they have a full season of
# 2025 sample (17 games vs a 4-game prior weight below).
# Sourced from general knowledge of 2020s-era NFL per-game position
# averages (a full-time RB1 running ~45 yds/g, a WR1 catching ~48 yds/g on
# ~4 catches, a TE1 ~30 yds/g, a starting QB ~1.3 pass TD/g).
# ---------------------------------------------------------------------------
POSITION_PRIORS: dict[str, dict[str, float]] = {
    "QB": {"rush_td": 0.12, "rec_td": 0.0, "pass_td": 1.30, "rush_yds": 9.0, "rec_yds": 0.0, "rec": 0.0, "pass_yds": 235.0},
    "RB": {"rush_td": 0.34, "rec_td": 0.08, "pass_td": 0.0, "rush_yds": 42.0, "rec_yds": 14.0, "rec": 2.1, "pass_yds": 0.0},
    "WR": {"rush_td": 0.02, "rec_td": 0.34, "pass_td": 0.0, "rush_yds": 2.0, "rec_yds": 46.0, "rec": 3.9, "pass_yds": 0.0},
    "TE": {"rush_td": 0.0, "rec_td": 0.24, "pass_td": 0.0, "rush_yds": 0.0, "rec_yds": 30.0, "rec": 3.1, "pass_yds": 0.0},
    "FB": {"rush_td": 0.10, "rec_td": 0.05, "pass_td": 0.0, "rush_yds": 10.0, "rec_yds": 6.0, "rec": 0.9, "pass_yds": 0.0},
}

# How many "games" of weight the position prior carries in the shrink blend.
# Matches value.py's bayesian_hit_rate default prior_strength of 4.
PRIOR_SAMPLE_WEIGHT = 4.0

MATCHUP_FLOOR = 0.75
MATCHUP_CEILING = 1.30


def shrunk_rate(observed_per_game: float, prior: float, sample_games: float, prior_weight: float = PRIOR_SAMPLE_WEIGHT) -> float:
    """Bayesian shrink of an observed per-game rate toward a position prior.

    sample_games=0 (no 2025 stats at all, e.g. an undrafted rookie) returns
    the prior untouched. A full 17-game 2025 season dominates the prior
    ~4.25:1.
    """
    sample_games = max(0.0, sample_games)
    return (observed_per_game * sample_games + prior * prior_weight) / max(sample_games + prior_weight, 1e-9)


def matchup_ratio(allowed_value: float, league_avg_allowed: float) -> float:
    """Opponent's allowed rate vs league average, clipped to a sane band.

    >1.0 means the opponent allows more than average at this stat (softer
    matchup, boosts the player's rate); <1.0 means a tougher matchup.
    """
    if league_avg_allowed <= 0:
        return 1.0
    return max(MATCHUP_FLOOR, min(MATCHUP_CEILING, allowed_value / league_avg_allowed))


def poisson_at_least(rate: float, threshold: int) -> float:
    """P(X >= threshold) for a Poisson(rate) count variable."""
    if threshold <= 0:
        return 1.0
    rate = max(0.0, rate)
    below = sum(math.exp(-rate) * (rate ** k) / math.factorial(k) for k in range(threshold))
    return clamp_probability(1.0 - below)


def normal_at_least(mean: float, line: float, *, std_ratio: float = 0.55, min_std: float = 15.0) -> float:
    """P(X >= line) approximating a yardage stat as Normal(mean, std).

    std scales with the mean (floored) rather than being fixed, since a
    zero/low-mean player (a blocking TE with 8 rec yds/g) shouldn't get an
    artificially tight distribution either.
    """
    mean = max(0.0, mean)
    std = max(min_std, mean * std_ratio)
    z = (line - mean) / (std * math.sqrt(2.0))
    return clamp_probability(0.5 * (1.0 - math.erf(z)))


def yardage_line(mean: float, *, round_to: int = 5, share: float = 0.82, minimum: int = 5) -> int:
    """Pick a 'beatable but not trivial' yardage threshold below the mean,
    rounded to a clean number the way a real prop line would be posted."""
    raw = max(minimum, mean * share)
    return max(minimum, int(round(raw / round_to) * round_to))


def clamp_probability(value: float, minimum: float = 0.01, maximum: float = 0.99) -> float:
    return max(minimum, min(maximum, float(value)))


def moneyline_win_probability(power_a: float, power_b: float, *, home_edge: float = 1.0, scale: float = 13.0) -> float:
    """Convert a point-differential power-rating gap into a win probability.

    Logistic curve on the composite power gap (season point-differential per
    game, blended with recent form and win rate — see team_power_rating),
    calibrated so a ~13-point composite edge lands near a 75% favorite, a
    reasonable single-game NFL spread-to-win-probability anchor.
    """
    gap = (power_a + home_edge) - power_b
    return clamp_probability(1.0 / (1.0 + math.exp(-gap / scale)))


def team_power_rating(*, point_diff_per_game: float, recent_point_diff_per_game: float, win_pct: float) -> float:
    """Blend full-season point differential, recent-form differential, and
    win rate into one power number in point-differential units."""
    blended_diff = point_diff_per_game * 0.55 + recent_point_diff_per_game * 0.30
    win_component = (win_pct - 0.5) * 12.0  # translate win% above/below .500 into ~point-diff units
    return blended_diff + win_component * 0.15


def project_td_lambda(*, rush_td_per_game: float, rec_td_per_game: float, sample_games: float, position: str, rush_matchup: float, rec_matchup: float) -> float:
    """Anytime-TD lambda (rush + rec combined) for a player, matchup-adjusted."""
    prior = POSITION_PRIORS.get(position, POSITION_PRIORS["RB"])
    rush_component = shrunk_rate(rush_td_per_game, prior["rush_td"], sample_games) * rush_matchup
    rec_component = shrunk_rate(rec_td_per_game, prior["rec_td"], sample_games) * rec_matchup
    return rush_component + rec_component


def project_pass_td_lambda(*, pass_td_per_game: float, sample_games: float, pass_matchup: float) -> float:
    prior = POSITION_PRIORS["QB"]["pass_td"]
    return shrunk_rate(pass_td_per_game, prior, sample_games) * pass_matchup


def project_pass_yds_mean(*, pass_yds_per_game: float, sample_games: float, pass_matchup: float) -> float:
    prior = POSITION_PRIORS["QB"]["pass_yds"]
    return shrunk_rate(pass_yds_per_game, prior, sample_games) * pass_matchup


def project_rush_yds_mean(*, rush_yds_per_game: float, sample_games: float, position: str, rush_matchup: float) -> float:
    prior = POSITION_PRIORS.get(position, POSITION_PRIORS["RB"])["rush_yds"]
    return shrunk_rate(rush_yds_per_game, prior, sample_games) * rush_matchup


def project_rec_yds_mean(*, rec_yds_per_game: float, sample_games: float, position: str, rec_matchup: float) -> float:
    prior = POSITION_PRIORS.get(position, POSITION_PRIORS["WR"])["rec_yds"]
    return shrunk_rate(rec_yds_per_game, prior, sample_games) * rec_matchup


def project_rec_rate(*, rec_per_game: float, sample_games: float, position: str, rec_matchup: float) -> float:
    prior = POSITION_PRIORS.get(position, POSITION_PRIORS["WR"])["rec"]
    return shrunk_rate(rec_per_game, prior, sample_games) * rec_matchup
