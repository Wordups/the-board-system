"""Value-pricing helpers shared by stat-line markets (NBA, WNBA, etc.).

The board's ranking principle: feature plays in the AIM (~ -100) and VALUE
(+100 to +1000) implied-odds zones. Reject anything chalkier than -400.
The line shipped per candidate is the one where the model's shrunken hit
rate lands closest to 0.50, not the line engineered to maximize hit rate.
"""

from __future__ import annotations

from typing import Any


# Implied-odds buckets (American odds → probability):
#   -400  ≈ 0.80   (chalk floor, reject above this)
#   -200  ≈ 0.667
#   -100  ≈ 0.50   (aim)
#   +100  ≈ 0.50
#   +400  ≈ 0.20
#  +1000  ≈ 0.0909
CHALK_PROB_FLOOR = 0.80
LONGSHOT_PROB_CEILING = 0.10
AIM_PROB_LOW = 0.40
AIM_PROB_HIGH = 0.60
VALUE_PROB_LOW = 0.20
VALUE_PROB_HIGH = 0.40


def bayesian_hit_rate(hits: int, n: int, prior_hit_rate: float, prior_strength: int = 4) -> float:
    """Shrink a small-sample hit rate toward a prior so 2/2 doesn't outscore 9/12."""
    if n <= 0:
        return prior_hit_rate
    return (hits + prior_strength * prior_hit_rate) / (n + prior_strength)


def hit_rate_to_implied_odds(p: float) -> int:
    """Convert hit-rate probability to American odds. 0.50 → -100. 0.25 → +300."""
    if p >= 0.999:
        return -10000
    if p <= 0.001:
        return 10000
    if p >= 0.50:
        return -int(round(100 * p / (1 - p)))
    return int(round(100 * (1 - p) / p))


def format_implied_odds(odds: int) -> str:
    return f"{odds:+d}" if odds > 0 else f"{odds}"


def value_zone(p: float) -> str:
    """Bucket a hit-rate probability into a value zone label.

    The chalk/longshot REJECTION has been removed — every probability now gets a
    descriptive label and is allowed onto the board. 'chalk' and 'longshot' are
    retained purely as labels (formerly the rejection bands); nothing is dropped
    on the basis of these zones anymore.
    """
    if p >= CHALK_PROB_FLOOR:
        return "chalk"
    if p >= AIM_PROB_HIGH:
        return "lean"
    if p >= AIM_PROB_LOW:
        return "aim"
    if p >= VALUE_PROB_LOW:
        return "value"
    return "longshot"


def find_value_line(
    *,
    market: str,
    recent_logs: list[dict[str, Any]],
    baseline: float,
    projection: float,
    line_minimums: dict[str, int],
    prior_hit_rate: float = 0.50,
    prior_strength: int = 4,
    line_ceiling_buffer: int = 5,
) -> dict[str, Any] | None:
    """Search the player's plausible line range and return the best line in
    the AIM/VALUE zone — the line closest to a 0.50 shrunken hit rate.

    Returns dict with keys: line, hit_rate, implied_odds, edge, zone — or
    None only if the player has no recent logs at all. The chalk/longshot
    rejection band (>=0.80 / <=0.10) has been REMOVED: every line in the
    player's range is now a valid candidate, so a line is no longer dropped
    for being too chalky or too long a shot.
    """
    if not recent_logs:
        return None
    floor = max(line_minimums.get(market, 1), int(round(baseline)))
    ceiling = max(floor + 4, int(round(projection)) + line_ceiling_buffer)
    n = len(recent_logs)
    candidates: list[dict[str, Any]] = []
    for line in range(floor, ceiling + 1):
        hits = sum(1 for log in recent_logs if log.get(market, 0) >= line)
        p = bayesian_hit_rate(hits, n, prior_hit_rate, prior_strength)
        candidates.append(
            {
                "line": line,
                "hit_rate": round(p, 4),
                "implied_odds": hit_rate_to_implied_odds(p),
                "edge": round(projection - line, 2),
                "zone": value_zone(p),
            }
        )
    if not candidates:
        return None
    # Prefer lines closest to 0.50 (the AIM zone). Tie-break: prefer the
    # one with the most positive edge (model-projection above the line).
    return min(candidates, key=lambda c: (abs(c["hit_rate"] - 0.50), -c["edge"]))


def is_marketable(
    *,
    sample_size: int,
    previous_avg: float,
    previous_avg_floor: float,
    minimum_current_sample: int = 5,
) -> bool:
    """Marketability gate: a player must either have a meaningful body of
    work last season at this stat threshold, OR have accumulated enough
    current-season games to be self-evidencing.
    """
    if sample_size >= minimum_current_sample:
        return True
    return previous_avg >= previous_avg_floor
