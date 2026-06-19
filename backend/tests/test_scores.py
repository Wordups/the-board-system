from __future__ import annotations

from app.models.mlb_model import MlbPlayCandidate
from app.scoring.edge_score import score_candidate


def _make_candidate(market: str = "HR", stat_value: float = 0.40, extra: dict | None = None) -> MlbPlayCandidate:
    return MlbPlayCandidate(
        player_id="x",
        player_name="Test Player",
        team="NYY",
        opponent="BOS",
        game_id="game",
        market=market,
        line=f"{market} 1+",
        stat_value=stat_value,
        baseline=0.22,
        trend=0.55,
        matchup=0.45,
        recent_form=0.50,
        extra=extra or {},
    )


def test_score_candidate_produces_confidence_and_tier():
    scored = score_candidate(_make_candidate())
    assert scored.score >= 0
    assert 0 <= scored.confidence <= 100
    assert scored.tier in {"A", "B", "C", "PASS"}


def test_score_candidate_in_0_100_range():
    # Fallback edge score must live in [0, 100].
    scored = score_candidate(_make_candidate(market="HR", stat_value=0.95))
    assert 0.0 <= scored.score <= 100.0


# Rule 48 (rookie tier cap) has been REMOVED. Career-game count no longer
# downgrades a tier or annotates the reason.
def test_rule48_removed_no_cap_for_rookie_hitter():
    candidate = _make_candidate(market="HR", stat_value=0.85, extra={"career_games": 3})
    scored = score_candidate(candidate)
    assert "Rookie cap" not in scored.reason


def test_rule48_removed_zero_career_games_not_capped():
    candidate = _make_candidate(market="Hits", stat_value=0.85, extra={"career_games": 0})
    scored = score_candidate(candidate)
    assert "Rookie cap" not in scored.reason
    assert "Career <1G" not in scored.reason
