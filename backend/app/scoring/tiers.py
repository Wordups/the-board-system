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


# NFL per-market cutoffs, on the same 0-100 sim-probability score scale as
# above. Reusing MLB's flat 35/22/12 scale as-is would misgrade NFL picks:
# MLB's cutoffs are anchored to HR's ~40% practical ceiling, but NFL market
# ceilings vary far more by market than any single MLB stat does:
#   - TD (anytime, rush+rec): a bellcow RB or alpha WR1 in a plus matchup can
#     clear 60-70%; anchoring A there (same logic as MLB's HR anchor).
#   - REC / RushYds / RecYds: the collector places the line itself near a
#     natural hit rate (yardage_line/REC-rate design), so these cluster in a
#     45-65% "beatable line" band by construction — cutoffs sit lower than
#     TD's raw-probability scale to match.
#   - PassTD: lower-probability by nature (1-2 TD lines against a ~1.3/g QB
#     prior) — cutoffs sit between REC-family and TD.
#   - ML: team win probability; a heavy favorite routinely clears 75-80%, so
#     this scale runs the highest of any NFL market, mirroring how other
#     sports treat ML as its own probability regime.
NFL_TIER_CUTOFFS: dict[str, dict[str, float]] = {
    "TD": {"A": 45.0, "B": 30.0, "C": 18.0},
    "REC": {"A": 62.0, "B": 50.0, "C": 38.0},
    "RushYds": {"A": 62.0, "B": 50.0, "C": 38.0},
    "RecYds": {"A": 62.0, "B": 50.0, "C": 38.0},
    "PassTD": {"A": 55.0, "B": 42.0, "C": 30.0},
    "ML": {"A": 68.0, "B": 56.0, "C": 45.0},
}


def assign_nfl_tier(score: float, market: str) -> str:
    cutoffs = NFL_TIER_CUTOFFS.get(market, {"A": A_CUTOFF, "B": B_CUTOFF, "C": C_CUTOFF})
    if score >= cutoffs["A"]:
        return "A"
    if score >= cutoffs["B"]:
        return "B"
    if score >= cutoffs["C"]:
        return "C"
    return "PASS"
