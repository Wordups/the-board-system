from __future__ import annotations

from app.models.mlb_model import MlbPlayCandidate
from app.scoring.confidence import to_confidence
from app.scoring.market_weights import MARKET_WEIGHTS
from app.scoring.tiers import assign_tier


def score_candidate(candidate: MlbPlayCandidate) -> MlbPlayCandidate:
    weight = MARKET_WEIGHTS.get(candidate.market, 1.0)
    probability_edge = max(candidate.stat_value - candidate.baseline, 0.0) * 100
    support = (
        candidate.trend * 0.35
        + candidate.matchup * 0.25
        + candidate.recent_form * 0.40
    ) * 100
    score = round((probability_edge * 0.62 + support * 0.38) * weight * 0.52, 2)
    candidate.score = score
    candidate.confidence = to_confidence(score)
    candidate.tier = assign_tier(score)
    candidate.reason = (
        f"Edge {probability_edge:.1f}, trend {candidate.trend:.2f}, "
        f"matchup {candidate.matchup:.2f}, form {candidate.recent_form:.2f}"
    )
    return candidate
