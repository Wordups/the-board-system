from __future__ import annotations

# Tier cutoffs on the 0-100 sim-probability score scale.
#
# The score is the model's simulated clear probability expressed as 0-100
# (39.4% HR -> 39.4). Cutoffs are anchored to that reality:
#   - ~40% is the practical ceiling for the hardest market (HR), so an elite HR
#     (e.g. Yordan Alvarez at 39.4%) must clear A. A = 35.
#   - B captures clearly-actionable probabilities (one-in-five-plus edges on hard
#     markets, comfortable favorites on easy ones). B = 22.
#   - C is the marginal-but-playable band. C = 12.
#   - Below 12 is PASS and never reaches a board.
A_CUTOFF = 35.0
B_CUTOFF = 22.0
C_CUTOFF = 12.0


def assign_tier(score: float) -> str:
    if score >= A_CUTOFF:
        return "A"
    if score >= B_CUTOFF:
        return "B"
    if score >= C_CUTOFF:
        return "C"
    return "PASS"
