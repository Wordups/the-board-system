from __future__ import annotations

from typing import Iterable

from app.models.mlb_model import MlbPlayCandidate


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
    }


def sorted_candidates(candidates: Iterable[MlbPlayCandidate]) -> list[MlbPlayCandidate]:
    return sorted(
        candidates,
        key=lambda item: (item.score, item.confidence, item.player_name),
        reverse=True,
    )
