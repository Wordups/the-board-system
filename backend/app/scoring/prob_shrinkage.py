"""Decile-based probability shrinkage — the calibration correction layer.

Source of truth: the 2026-07-18 backtest (backend/backtest/harness.py,
19,044 picks graded 2026-05-27..2026-07-17). Findings this module encodes:

- Pooled across sports, every model-prob decile >= 0.50 replayed inflated
  (model prob minus realized hit rate, in probability points):
  0.5-0.6 +8.5pp, 0.6-0.7 +21.6pp, 0.7-0.8 +17.4pp, 0.8-0.9 +24.4pp,
  0.9+ +42.4pp. Deciles below 0.50 replayed trustworthy — no shrink.
- MLB Hits was the worst single bucket (+32.4pp): quarantined entirely —
  a quarantined market can never stamp BET, whatever its edge.
- WNBA replayed slightly UNDERconfident (-2pp pooled; WNBA ML -10.8pp):
  exempt from shrinkage. (No boost either — conservative.)

Pure math, no I/O, no pipeline imports: fully unit-testable and safe to
import from any stamping surface (kalshi_edge ML overlay, ladder rungs,
future scanner-side checks).

Target location: backend/app/scoring/prob_shrinkage.py
"""

from __future__ import annotations

from typing import Any

CALIBRATION_TABLE_ID = "pooled-backtest-2026-07-18"

# Inflation per fixed-width model-prob decile (probability points to subtract).
# Deciles 0-4 (< 0.50) replayed trustworthy: no entry, no shrink.
# NOTE: the 0.7-0.8 value is the real pooled gap_pp from the 2026-07-18
# offline harness rerun (n=523, avg model 76.3% vs 58.9% hit rate) — it
# replaces the 23.0 neighbor-interpolation placeholder the build shift
# staged. The table is genuinely non-monotonic: 0.7-0.8 replayed less
# inflated than 0.6-0.7.
SHRINK_PP_BY_DECILE: dict[int, float] = {
    5: 8.5,
    6: 21.6,
    7: 17.4,
    8: 24.4,
    9: 42.4,
}

# Sports whose sims replayed calibrated-or-under: pass through untouched.
NO_SHRINK_SPORTS = frozenset({"WNBA"})

# (sport, market) buckets too broken to trade at any edge. Stamping surfaces
# must emit QUARANTINE_DECISION for these instead of BET/PASS/CHECK.
QUARANTINED_MARKETS = frozenset({("MLB", "Hits")})
QUARANTINE_DECISION = "QUARANTINED"


def decile_index(prob: float) -> int:
    """Fixed-width decile bin: [0, 0.1) -> 0 ... [0.9, 1.0] -> 9.

    Mirrors backend/backtest/calibration.py so shrinkage bins line up with
    the harness's calibration tables.
    """
    prob = min(max(float(prob), 0.0), 1.0)
    return min(int(prob * 10.0), 9)


def shrink_pp(model_prob: float, sport: str | None = None) -> float:
    """Probability points to subtract from a raw model prob for this sport."""
    if sport is not None and str(sport).upper() in NO_SHRINK_SPORTS:
        return 0.0
    return SHRINK_PP_BY_DECILE.get(decile_index(model_prob), 0.0)


def is_quarantined(sport: str | None, market: str | None) -> bool:
    """True when (sport, market) may never stamp BET. Sport is
    case-insensitive; market keys are exact board keys ("Hits", "HR", ...)."""
    if sport is None or market is None:
        return False
    return (str(sport).upper(), str(market)) in QUARANTINED_MARKETS


def calibrate_prob(
    model_prob: float | None,
    *,
    sport: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    """Shrink a raw model probability per the pooled calibration table.

    Returns a dict (JSON-ready for board output):
      model_prob      calibrated probability (what stamping must use)
      model_prob_raw  the untouched input
      shrink_pp       points subtracted (0.0 when exempt/trustworthy)
      quarantined     True when this (sport, market) may never stamp BET
      table           CALIBRATION_TABLE_ID provenance tag

    None passes through (model_prob None, shrink_pp 0.0) so callers keep
    their existing missing-prob handling.
    """
    quarantined = is_quarantined(sport, market)
    if model_prob is None:
        return {
            "model_prob": None,
            "model_prob_raw": None,
            "shrink_pp": 0.0,
            "quarantined": quarantined,
            "table": CALIBRATION_TABLE_ID,
        }
    raw = min(max(float(model_prob), 0.0), 1.0)
    pp = shrink_pp(raw, sport)
    adjusted = min(max(raw - pp / 100.0, 0.0), 1.0)
    return {
        "model_prob": round(adjusted, 4),
        "model_prob_raw": round(raw, 4),
        "shrink_pp": pp,
        "quarantined": quarantined,
        "table": CALIBRATION_TABLE_ID,
    }
