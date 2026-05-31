from __future__ import annotations

from typing import Iterable

from app.models.mlb_model import MlbPlayCandidate
from app.sim.sim_engine import sim_prob_pct


def is_publishable_candidate(candidate: MlbPlayCandidate) -> bool:
    """True iff the candidate clears both filters: tier != PASS and the
    calibration guardrail hasn't quarantined it. The guardrail flag
    (calibration_status == 'flag') means the sim_prob is inflated above the
    closed-form market baseline past the configured threshold."""
    if candidate.tier == "PASS":
        return False
    if (candidate.extra or {}).get("held_for_calibration"):
        return False
    return True


def to_player_row(candidate: MlbPlayCandidate) -> dict:
    extra = candidate.extra or {}
    row = {
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
    cal_status = extra.get("calibration_status")
    if cal_status:
        row["calibration_status"] = cal_status
        if "baseline_prob_pct" in extra:
            row["baseline_prob_pct"] = extra["baseline_prob_pct"]
        if "calibration_gap_pp" in extra:
            row["calibration_gap_pp"] = extra["calibration_gap_pp"]
    return row


def sorted_candidates(candidates: Iterable[MlbPlayCandidate]) -> list[MlbPlayCandidate]:
    return sorted(
        candidates,
        key=lambda item: (item.score, item.confidence, item.player_name),
        reverse=True,
    )
