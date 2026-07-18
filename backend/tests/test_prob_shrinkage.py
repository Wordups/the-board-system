"""Tests for the decile-shrinkage calibration layer.

Target location: backend/tests/test_prob_shrinkage.py
The last three tests exercise the kalshi_edge wiring and only pass once the
kalshi_edge_calibration patch is applied (they fail loudly, not silently,
against unpatched code — by design).
"""

import pytest

from app.scoring.prob_shrinkage import (
    CALIBRATION_TABLE_ID,
    QUARANTINE_DECISION,
    SHRINK_PP_BY_DECILE,
    calibrate_prob,
    decile_index,
    is_quarantined,
    shrink_pp,
)


# ---------- decile binning (must mirror backend/backtest/calibration.py) ----

def test_decile_index_boundaries():
    assert decile_index(0.0) == 0
    assert decile_index(0.0999) == 0
    assert decile_index(0.1) == 1
    assert decile_index(0.4999) == 4
    assert decile_index(0.5) == 5
    assert decile_index(0.9) == 9
    assert decile_index(1.0) == 9  # top-inclusive, matches backtest binning


def test_decile_index_clamps_out_of_range():
    assert decile_index(-0.2) == 0
    assert decile_index(1.7) == 9


# ---------- shrink table shape ---------------------------------------------

def test_no_shrink_below_half():
    for prob in (0.05, 0.30, 0.449, 0.4999):
        assert shrink_pp(prob) == 0.0


def test_pooled_table_values():
    assert shrink_pp(0.55) == 8.5
    assert shrink_pp(0.65) == 21.6
    assert shrink_pp(0.75) == 17.4  # real pooled gap_pp from 7/18 harness rerun
    assert shrink_pp(0.85) == 24.4
    assert shrink_pp(0.95) == 42.4


def test_shrink_table_shape():
    # The real pooled table is non-monotonic (0.7-0.8 replayed less inflated
    # than 0.6-0.7), so no monotonicity assert — instead: every shrunk decile
    # subtracts something, and the 0.9+ decile is the worst offender.
    values = [SHRINK_PP_BY_DECILE[d] for d in sorted(SHRINK_PP_BY_DECILE)]
    assert all(v > 0.0 for v in values)
    assert SHRINK_PP_BY_DECILE[9] == max(values)


def test_wnba_exempt_at_every_decile():
    for prob in (0.55, 0.65, 0.75, 0.85, 0.95):
        assert shrink_pp(prob, sport="WNBA") == 0.0
        assert shrink_pp(prob, sport="wnba") == 0.0  # case-insensitive


def test_mlb_shrinks():
    assert shrink_pp(0.582, sport="MLB") == 8.5
    assert shrink_pp(0.927, sport="MLB") == 42.4


# ---------- quarantine ------------------------------------------------------

def test_mlb_hits_quarantined():
    assert is_quarantined("MLB", "Hits") is True
    assert is_quarantined("mlb", "Hits") is True


def test_other_buckets_not_quarantined():
    assert is_quarantined("MLB", "HR") is False
    assert is_quarantined("MLB", "TB") is False
    assert is_quarantined("WNBA", "PTS") is False
    assert is_quarantined(None, "Hits") is False
    assert is_quarantined("MLB", None) is False


# ---------- calibrate_prob --------------------------------------------------

def test_calibrate_prob_shrinks_and_reports():
    out = calibrate_prob(0.582, sport="MLB", market="ML")
    assert out["model_prob"] == pytest.approx(0.497, abs=1e-4)
    assert out["model_prob_raw"] == pytest.approx(0.582, abs=1e-4)
    assert out["shrink_pp"] == 8.5
    assert out["quarantined"] is False
    assert out["table"] == CALIBRATION_TABLE_ID


def test_calibrate_prob_backtest_headline_decile():
    # 0.9+ decile claimed 92.7%, hit 50.3% — shrunk quote must land ~50%.
    out = calibrate_prob(0.927, sport="MLB", market="ML")
    assert out["model_prob"] == pytest.approx(0.503, abs=1e-4)


def test_calibrate_prob_wnba_untouched():
    out = calibrate_prob(0.62, sport="WNBA", market="PTS")
    assert out["model_prob"] == pytest.approx(0.62, abs=1e-9)
    assert out["shrink_pp"] == 0.0


def test_calibrate_prob_none_passthrough():
    out = calibrate_prob(None, sport="MLB", market="ML")
    assert out["model_prob"] is None
    assert out["shrink_pp"] == 0.0


def test_calibrate_prob_clamps():
    out = calibrate_prob(1.4, sport="MLB", market="ML")
    assert 0.0 <= out["model_prob"] <= 1.0
    assert out["model_prob_raw"] == 1.0


# ---------- kalshi_edge wiring (require the calibration patch) --------------

def test_ml_stamp_uses_shrunk_prob():
    """The backtest's average bet: model 58.2% vs market 49.3% was a BET
    pre-recal; shrunk to 49.7% the edge is +0.4pp -> PASS."""
    from app.builders.kalshi_edge import build_kalshi_block, decide_pick

    row = {"sim_prob_pct": 58.2}
    summary = {"ticker": "KXMLBGAME-TEST", "implied_prob": 0.493, "volume": 100}
    block = build_kalshi_block(row, summary, sport="MLB")
    assert block["model_prob"] == pytest.approx(0.497, abs=1e-3)
    assert block["model_prob_raw"] == pytest.approx(0.582, abs=1e-3)
    assert decide_pick(block["edge_pp"], block["implied_prob"]) == "PASS"


def test_wnba_ladder_rung_still_bets():
    from app.builders.kalshi_edge import build_ladder_rung

    summary = {"ticker": "KXWNBAPTS-TEST", "implied_prob": 0.50, "volume": 50}
    rung = build_ladder_rung(20, 0.62, summary, sport="WNBA", market="PTS")
    assert rung["model_prob"] == pytest.approx(0.62, abs=1e-9)
    assert rung["decision"] == "BET"


def test_mlb_hits_rung_quarantined_never_bets():
    from app.builders.kalshi_edge import build_ladder_rung

    summary = {"ticker": "KXMLBHITS-TEST", "implied_prob": 0.40, "volume": 50}
    rung = build_ladder_rung(1, 0.90, summary, sport="MLB", market="Hits")
    assert rung["decision"] == QUARANTINE_DECISION
    assert rung["decision"] != "BET"
