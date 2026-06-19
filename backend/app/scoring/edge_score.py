from __future__ import annotations

from app.models.mlb_model import MlbPlayCandidate
from app.scoring.confidence import to_confidence
from app.scoring.market_weights import MARKET_WEIGHTS
from app.scoring.tiers import assign_tier
from app.sim.sim_engine import sim_prob_to_score


def edge_score_to_score(edge_score: float) -> float:
    """Fallback 0-100 score for candidates with no sim probability.

    The legacy edge score lived in a ~0-50 band; double it (clamped) so a
    candidate without a sim_prob still produces a comparable 0-100 number
    rather than crashing or zeroing the board. This is a graceful fallback,
    not the primary path — the sim probability is the foundational score.
    """
    return round(max(0.0, min(100.0, float(edge_score) * 2.0)), 2)


def score_candidate(candidate: MlbPlayCandidate) -> MlbPlayCandidate:
    """Compute the edge-based score and reason.

    Runs BEFORE the Monte Carlo sim (sim_prob is not yet available), so the
    0-100 score it sets here is the *fallback* derived from the edge model.
    Once the sim has run, ``rescore_with_sim`` overwrites the score with the
    simulated probability where one exists — the probability IS the score.
    """
    weight = MARKET_WEIGHTS.get(candidate.market, 1.0)
    probability_edge = max(candidate.stat_value - candidate.baseline, 0.0) * 100
    support_weights = support_weights_for(candidate.market)
    support = (
        candidate.trend * support_weights["trend"]
        + candidate.matchup * support_weights["matchup"]
        + candidate.recent_form * support_weights["recent_form"]
    ) * 100
    raw_edge = round((probability_edge * 0.62 + support * 0.38) * weight * 0.52, 2)
    if candidate.market == "HR":
        raw_edge = round(apply_hr_quality_adjustments(candidate, raw_edge), 2)
    score = edge_score_to_score(raw_edge)
    score = round(apply_availability_adjustments(candidate, score), 2)
    candidate.score = score
    candidate.confidence = to_confidence(score)
    candidate.tier = assign_tier(score)

    candidate.reason = build_reason(
        candidate=candidate,
        probability_edge=probability_edge,
        support_weights=support_weights,
    )
    return candidate


def rescore_with_sim(candidate: MlbPlayCandidate) -> MlbPlayCandidate:
    """Make the simulated clear probability the foundational 0-100 score.

    Called after ``simulate_candidates`` has populated ``sim_prob``. Where a
    sim probability exists, the score becomes that probability on a 0-100 scale
    (39.4% HR -> 39.4); confidence and tier are re-derived from it. The lineup-
    uncertainty penalty is re-applied on top so availability risk still bites.

    Candidates without a sim_prob keep the edge-based fallback score from
    ``score_candidate`` untouched.
    """
    sim_score = sim_prob_to_score(getattr(candidate, "sim_prob", None))
    if sim_score is None:
        return candidate
    score = round(apply_availability_adjustments(candidate, sim_score), 2)
    candidate.score = score
    candidate.confidence = to_confidence(score)
    candidate.tier = assign_tier(score)
    return candidate


def apply_availability_adjustments(candidate: MlbPlayCandidate, score: float) -> float:
    extra = candidate.extra or {}
    lineup_penalty = float(extra.get("lineup_uncertainty_penalty", 0.0) or 0.0)
    return max(score - lineup_penalty, 0.0)


def apply_hr_quality_adjustments(candidate: MlbPlayCandidate, score: float) -> float:
    extra = candidate.extra or {}
    projected_pa = float(extra.get("projected_pa", 0.0) or 0.0)
    order_estimate = int(extra.get("order_estimate", 9) or 9)
    hr_power_index = float(extra.get("hr_power_index", 0.0) or 0.0)
    power_surge = float(extra.get("power_surge", 0.0) or 0.0)
    power_boost = float(extra.get("power_boost", 0.0) or 0.0)
    platoon_edge = float(extra.get("platoon_edge", 0.0) or 0.0)
    pitcher_hr9 = float(extra.get("pitcher_hr9", 0.0) or 0.0)
    historical_hr_probability = float(extra.get("historical_hr_probability", 0.0) or 0.0)
    season_hr_probability = float(extra.get("season_hr_probability", 0.0) or 0.0)
    sample_reliability = float(extra.get("sample_reliability", 0.0) or 0.0)

    score += hr_power_index * 5.4
    score += power_surge * 4.6
    if projected_pa >= 4.25:
        score += 1.4
    elif projected_pa <= 3.55:
        score -= 1.8
    if order_estimate >= 7:
        score -= 1.6
    elif order_estimate <= 4:
        score += 1.1
    if pitcher_hr9 >= 1.35:
        score += 1.4
    elif pitcher_hr9 and pitcher_hr9 <= 0.85:
        score -= 1.3
    if power_boost <= 0.24 and historical_hr_probability <= 0.17 and season_hr_probability <= 0.18:
        score -= 2.1
    if sample_reliability < 0.42 and power_surge < 0.3:
        score -= 1.1
    if platoon_edge >= 0.72:
        score += 0.9
    return max(score, 0.0)


def support_weights_for(market: str) -> dict[str, float]:
    if market == "HR":
        return {
            "trend": 0.42,
            "matchup": 0.28,
            "recent_form": 0.30,
        }
    if market == "RBI":
        return {
            "trend": 0.34,
            "matchup": 0.33,
            "recent_form": 0.33,
        }
    return {
        "trend": 0.35,
        "matchup": 0.25,
        "recent_form": 0.40,
    }


def build_reason(*, candidate: MlbPlayCandidate, probability_edge: float, support_weights: dict[str, float]) -> str:
    if candidate.market == "HR":
        return build_hr_reason(candidate, probability_edge, support_weights)
    if candidate.market == "RBI":
        return build_rbi_reason(candidate, probability_edge, support_weights)
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
    pitcher_name = extra.get("pitcher_name", "")
    pitcher_era = extra.get("pitcher_era", 0.0)
    pitcher_whip = extra.get("pitcher_whip", 0.0)
    pitcher_hr9 = extra.get("pitcher_hr9", 0.0)
    pitcher_hr_allowed = extra.get("pitcher_hr_allowed", 0)
    pitcher_hand = extra.get("pitcher_hand", "")
    projected_pa = extra.get("projected_pa", 0.0)
    sample_reliability = extra.get("sample_reliability", 0.0)
    recent_peak_hr_rate = extra.get("recent_peak_hr_rate", 0.0)
    unlucky_power_index = extra.get("unlucky_power_index", 0.0)
    rising_star_index = extra.get("rising_star_index", 0.0)
    hr_power_index = extra.get("hr_power_index", 0.0)
    power_surge = extra.get("power_surge", 0.0)
    l5_xbh_per_game = extra.get("l5_xbh_per_game", 0.0)
    season_xbh_per_game = extra.get("season_xbh_per_game", 0.0)
    age = extra.get("age", 0)
    platoon_edge = extra.get("platoon_edge", 0.0)
    vs_pitcher_avg = extra.get("vs_pitcher_avg", 0.0)
    vs_pitcher_ops = extra.get("vs_pitcher_ops", 0.0)
    vs_pitcher_hr = extra.get("vs_pitcher_hr", 0)
    vs_pitcher_pa = extra.get("vs_pitcher_pa", 0)
    lineup_confirmed = extra.get("lineup_confirmed")
    lineup_uncertainty_penalty = extra.get("lineup_uncertainty_penalty", 0.0)
    player_status = extra.get("player_status", "")

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
        f"PowIdx {hr_power_index:.2f}",
        f"Surge {power_surge:.2f}",
        f"Matchup {pitcher_matchup:.2f}",
        f"ProjPA {projected_pa:.1f}",
        f"Sample {sample_reliability:.2f}",
        f"Peak3Y {recent_peak_hr_rate:.3f}",
    ]
    if pitcher_name:
        reasons.append(f"vs {pitcher_name}")
    if pitcher_era:
        reasons.append(f"ERA {pitcher_era:.2f}")
    if pitcher_whip:
        reasons.append(f"WHIP {pitcher_whip:.2f}")
    if pitcher_hr9:
        reasons.append(f"HR/9 {pitcher_hr9:.2f}")
    if pitcher_hr_allowed:
        reasons.append(f"HR A {int(pitcher_hr_allowed)}")
    if pitcher_hand:
        reasons.append(f"Hand {pitcher_hand}")
    if platoon_edge >= 0.7:
        reasons.append("Platoon +")
    elif platoon_edge > 0:
        reasons.append("Platoon -")
    if vs_pitcher_pa:
        reasons.append(f"vsP {vs_pitcher_avg:.3f}/{vs_pitcher_ops:.3f} in {int(vs_pitcher_pa)} PA")
    if vs_pitcher_hr:
        reasons.append(f"vsP HR {int(vs_pitcher_hr)}")
    if age:
        reasons.append(f"Age {age}")
    if order_estimate:
        reasons.append(f"Order est. {order_estimate}")
    if lineup_confirmed:
        reasons.append("Lineup confirmed")
    elif lineup_uncertainty_penalty:
        reasons.append(f"Lineup pen {float(lineup_uncertainty_penalty):.1f}")
    if player_status and player_status != "Active":
        reasons.append(f"Status {player_status}")
    if unlucky_power_index >= 0.18:
        reasons.append(f"Power due {unlucky_power_index:.2f}")
    if rising_star_index >= 0.22:
        reasons.append(f"Rising {rising_star_index:.2f}")
    if l5_xbh_per_game:
        reasons.append(f"L5 XBH {float(l5_xbh_per_game):.2f}/g")
    if season_xbh_per_game:
        reasons.append(f"Season XBH {float(season_xbh_per_game):.2f}/g")
    weights = (
        f"Wts T{support_weights['trend']:.2f}"
        f"/M{support_weights['matchup']:.2f}"
        f"/F{support_weights['recent_form']:.2f}"
    )
    reasons.append(weights)
    return " | ".join(reasons)


def build_rbi_reason(candidate: MlbPlayCandidate, probability_edge: float, support_weights: dict[str, float]) -> str:
    extra = candidate.extra or {}
    return " | ".join(
        [
            f"RBI edge {probability_edge:.1f}",
            f"L5 {float(extra.get('l5_rbi_per_game', 0.0)):.2f}/g",
            f"L10 {float(extra.get('l10_rbi_per_game', 0.0)):.2f}/g",
            f"Season {float(extra.get('season_rbi_per_game', 0.0)):.2f}/g",
            f"Order est. {int(extra.get('order_estimate', 0) or 0)}" if extra.get("order_estimate") else "Order est. -",
            f"Team R/G {float(extra.get('team_runs_per_game', 0.0)):.2f}",
            f"Team OBP {float(extra.get('team_obp', 0.0)):.3f}",
            f"WHIP {float(extra.get('pitcher_whip', 0.0)):.2f}" if extra.get("pitcher_whip") else "WHIP -",
            f"Platoon {float(extra.get('platoon_edge', 0.0)):.2f}",
            f"RISP {float(extra.get('risp_signal', 0.0)):.2f}",
            f"Lineup pen {float(extra.get('lineup_uncertainty_penalty', 0.0)):.1f}" if extra.get("lineup_uncertainty_penalty") else "Lineup pen 0.0",
            f"Wts T{support_weights['trend']:.2f}/M{support_weights['matchup']:.2f}/F{support_weights['recent_form']:.2f}",
        ]
    )
