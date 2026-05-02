from __future__ import annotations

from app.models.mlb_model import MlbPlayCandidate
from app.scoring.confidence import to_confidence
from app.scoring.market_weights import MARKET_WEIGHTS
from app.scoring.tiers import assign_tier


def score_candidate(candidate: MlbPlayCandidate) -> MlbPlayCandidate:
    weight = MARKET_WEIGHTS.get(candidate.market, 1.0)
    probability_edge = max(candidate.stat_value - candidate.baseline, 0.0) * 100
    support_weights = support_weights_for(candidate.market)
    support = (
        candidate.trend * support_weights["trend"]
        + candidate.matchup * support_weights["matchup"]
        + candidate.recent_form * support_weights["recent_form"]
    ) * 100
    score = round((probability_edge * 0.62 + support * 0.38) * weight * 0.52, 2)
    candidate.score = score
    candidate.confidence = to_confidence(score)
    candidate.tier = assign_tier(score)
    candidate.reason = build_reason(
        candidate=candidate,
        probability_edge=probability_edge,
        support_weights=support_weights,
    )
    return candidate


def support_weights_for(market: str) -> dict[str, float]:
    if market == "HR":
        return {
            "trend": 0.42,
            "matchup": 0.23,
            "recent_form": 0.35,
        }
    return {
        "trend": 0.35,
        "matchup": 0.25,
        "recent_form": 0.40,
    }


def build_reason(*, candidate: MlbPlayCandidate, probability_edge: float, support_weights: dict[str, float]) -> str:
    if candidate.market == "HR":
        return build_hr_reason(candidate, probability_edge, support_weights)
    return (
        f"Edge {probability_edge:.1f}, trend {candidate.trend:.2f}, "
        f"matchup {candidate.matchup:.2f}, form {candidate.recent_form:.2f}"
    )


def build_hr_reason(candidate: MlbPlayCandidate, probability_edge: float, support_weights: dict[str, float]) -> str:
    extra = candidate.extra or {}
    order_estimate = extra.get("order_estimate")
    ops = extra.get("ops", 0.0)
    slg = extra.get("slg", 0.0)
    iso = extra.get("iso", 0.0)
    season_hr = extra.get("season_hr_per_game", 0.0)
    l10_hr = extra.get("l10_hr_per_game", 0.0)
    l5_hr = extra.get("l5_hr_per_game", 0.0)
    season_hr_probability = extra.get("season_hr_probability", 0.0)
    historical_hr_probability = extra.get("historical_hr_probability", 0.0)
    pitcher_matchup = extra.get("pitcher_matchup", candidate.matchup)
    projected_pa = extra.get("projected_pa", 0.0)
    sample_reliability = extra.get("sample_reliability", 0.0)
    recent_peak_hr_rate = extra.get("recent_peak_hr_rate", 0.0)
    unlucky_power_index = extra.get("unlucky_power_index", 0.0)
    rising_star_index = extra.get("rising_star_index", 0.0)
    age = extra.get("age", 0)

    reasons = [
        f"HR edge {probability_edge:.1f}",
        f"L5 {l5_hr:.2f}/g",
        f"L10 {l10_hr:.2f}/g",
        f"Season {season_hr:.2f}/g",
        f"HR% {season_hr_probability:.2f}",
        f"HistHR% {historical_hr_probability:.2f}",
        f"OPS {ops:.3f}",
        f"SLG {slg:.3f}",
        f"ISO {iso:.3f}",
        f"Matchup {pitcher_matchup:.2f}",
        f"ProjPA {projected_pa:.1f}",
        f"Sample {sample_reliability:.2f}",
        f"Peak3Y {recent_peak_hr_rate:.3f}",
    ]
    if age:
        reasons.append(f"Age {age}")
    if order_estimate:
        reasons.append(f"Order est. {order_estimate}")
    if unlucky_power_index >= 0.18:
        reasons.append(f"Power due {unlucky_power_index:.2f}")
    if rising_star_index >= 0.22:
        reasons.append(f"Rising {rising_star_index:.2f}")
    weights = (
        f"Wts T{support_weights['trend']:.2f}"
        f"/M{support_weights['matchup']:.2f}"
        f"/F{support_weights['recent_form']:.2f}"
    )
    reasons.append(weights)
    return " | ".join(reasons)
