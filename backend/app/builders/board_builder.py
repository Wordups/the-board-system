from __future__ import annotations

from typing import Iterable

from app.models.mlb_model import MlbPlayCandidate
from app.sim.sim_engine import sim_prob_pct


def to_player_row(candidate: MlbPlayCandidate) -> dict:
    return {
        "player_id": candidate.player_id,
        "player_name": candidate.player_name,
        "team": candidate.team,
        "opponent": candidate.opponent,
        "line": candidate.line,
        "score": candidate.score,
        "confidence": candidate.confidence,
        "tier": candidate.tier,
        "reason": candidate.reason,
        "sim_prob_pct": sim_prob_pct(candidate),
    }


def sorted_candidates(candidates: Iterable[MlbPlayCandidate]) -> list[MlbPlayCandidate]:
    return sorted(
        candidates,
        key=lambda item: (item.score, item.confidence, item.player_name),
        reverse=True,
    )
