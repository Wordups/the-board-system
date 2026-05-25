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
    assert scored.score > 0
    assert 1 <= scored.confidence <= 99
    assert scored.tier in {"A", "B", "C", "PASS"}


# Rule 48: rookie tier cap tests
def test_rule48_caps_hitter_with_few_career_games():
    # High edge HR play but only 3 career MLB games → must be capped at C
    candidate = _make_candidate(market="HR", stat_value=0.85, extra={"career_games": 3})
    scored = score_candidate(candidate)
    assert scored.tier == "C", f"Expected C-tier rookie cap, got {scored.tier} (score={scored.score})"
    assert "Rookie cap" in scored.reason


def test_rule48_does_not_cap_veteran_hitter():
    # 50 career games → no cap regardless of tier
    candidate = _make_candidate(market="HR", stat_value=0.85, extra={"career_games": 50})
    scored = score_candidate(candidate)
    assert "Rookie cap" not in scored.reason


def test_rule48_does_not_cap_k_pitcher_market():
    # K market — career_games is not set → None → no cap (prevents false positive on pitchers)
    candidate = _make_candidate(market="K", stat_value=0.85, extra={})
    scored = score_candidate(candidate)
    assert "Rookie cap" not in scored.reason, f"K market falsely capped: {scored.reason}"


def test_rule48_exactly_25_games_is_not_capped():
    # Boundary: exactly 25 games is NOT a rookie (< 25 strict)
    candidate = _make_candidate(market="HR", stat_value=0.85, extra={"career_games": 25})
    scored = score_candidate(candidate)
    assert "Rookie cap" not in scored.reason


def test_rule48_career_games_zero_is_capped():
    # 0 career games still triggers cap and uses "Career <1G" label
    candidate = _make_candidate(market="Hits", stat_value=0.85, extra={"career_games": 0})
    scored = score_candidate(candidate)
    assert scored.tier == "C"
    assert "Career <1G" in scored.reason
