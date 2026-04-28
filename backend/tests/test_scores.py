from __future__ import annotations

from app.models.mlb_model import MlbPlayCandidate
from app.scoring.edge_score import score_candidate


def test_score_candidate_produces_confidence_and_tier():
    candidate = MlbPlayCandidate(
        player_id="x",
        player_name="Test Player",
        team="NYY",
        opponent="BOS",
        game_id="game",
        market="HR",
        line="HR 1+",
        stat_value=0.40,
        baseline=0.22,
        trend=0.11,
        matchup=0.09,
        recent_form=0.12,
    )
    scored = score_candidate(candidate)
    assert scored.score > 0
    assert 1 <= scored.confidence <= 99
    assert scored.tier in {"A", "B", "C", "PASS"}
