"""Tests for the calibration guardrail — math, market mapping, and the
build-time gate that runs in mlb_board_builder."""

from __future__ import annotations

import math

from app.models.mlb_model import MlbPlayCandidate
from app.scoring import calibration_guardrail as cg
from app.builders.mlb_board_builder import apply_calibration_gate
from app.builders.board_builder import is_publishable_candidate


# ---------- baseline math ----------

def test_p_at_least_1_hit_matches_closed_form():
    # P(>=1 hit) for BA=0.250 over 4 AB = 1 - 0.75^4 = 0.68359...
    assert math.isclose(
        cg.p_at_least_k_hits(0.250, 4, 1), 1 - (1 - 0.250) ** 4, abs_tol=1e-9
    )


def test_p_at_least_2_hits_strictly_below_1_hit():
    # Sanity: for any AB and BA in (0,1), P(>=2) < P(>=1). This is the math the
    # threshold-bug diagnostic relies on.
    for ba in (0.220, 0.250, 0.280, 0.310):
        p1 = cg.p_at_least_k_hits(ba, 4, 1)
        p2 = cg.p_at_least_k_hits(ba, 4, 2)
        assert p2 < p1
        # And the gap should be substantial — at BA .250, ~42pp.
        assert p1 - p2 > 0.30


def test_hr_baseline_matches_geometric_form():
    # P(>=1 HR) = 1 - (1-hr_per_pa)^pa
    assert math.isclose(cg.p_at_least_1_hr(0.040, 4), 1 - 0.96 ** 4, abs_tol=1e-9)


def test_tb_dist_distribution_sums_to_one():
    # Whatever season line we feed in, the per-AB distribution must sum to 1.
    d = cg.tb_dist_from_line(ba=0.275, ab=500, doubles=30, triples=2, hr=20)
    assert math.isclose(sum(d.values()), 1.0, abs_tol=1e-9)


def test_strikeout_baseline_zero_when_lambda_zero():
    assert cg.p_at_least_k_strikeouts(0.0, 6.0, 9) == 0.0
    assert cg.p_at_least_k_strikeouts(10.0, 0.0, 9) == 0.0


# ---------- market key mapping ----------

def test_market_key_from_board_covers_live_markets():
    assert cg.market_key_from_board("HR", "HR 1+") == "hr_1"
    assert cg.market_key_from_board("Hits", "1+ Hit") == "hits_1"
    assert cg.market_key_from_board("Hits", "2+ Hits") == "hits_2"
    assert cg.market_key_from_board("TB", "2+ TB") == "tb_2"
    assert cg.market_key_from_board("K", "9+ K") == "k_9"
    assert cg.market_key_from_board("RBI", "2+ RBI") == "rbi_2"
    # Markets the guardrail does not model:
    assert cg.market_key_from_board("ML", "Moneyline") is None
    assert cg.market_key_from_board("RBI", "1+ RBI") is None  # no rbi_1 baseline


# ---------- status classifier (hard vs soft) ----------

def test_hard_market_flags_when_gap_exceeds_threshold():
    assert cg.status_for("hits_2", gap=0.20, threshold=0.15) == "flag"


def test_rbi_soft_warn_never_quarantines():
    # rbi_2 is in SOFT_MARKETS — even an absurd gap returns 'warn', not 'flag'.
    assert cg.status_for("rbi_2", gap=0.50, threshold=0.15) == "warn"


def test_negative_gap_never_flags():
    # Sim below baseline = appropriately humble; let through.
    assert cg.status_for("hits_2", gap=-0.10, threshold=0.15) == "ok"


# ---------- the spec's three canonical cases ----------

def test_goodman_2plus_hits_flags_inflation():
    play = cg.Play("Hunter Goodman", "hits_2", sim_prob=0.941, ba=0.242, ab_per_game=4.0)
    rows = cg.score_board([play])
    assert rows[0]["flag"] is True
    assert rows[0]["gap"] > 0.50  # gap pp is enormous (~69)


def test_calibrated_sim_passes():
    play = cg.Play("Calibrated 1+H", "hits_1", sim_prob=0.730, ba=0.280, ab_per_game=4.0)
    rows = cg.score_board([play])
    assert rows[0]["status"] == "ok"


def test_inflated_1plus_hits_flags():
    play = cg.Play("Inflated 1+H", "hits_1", sim_prob=0.940, ba=0.280, ab_per_game=4.0)
    rows = cg.score_board([play])
    assert rows[0]["flag"] is True


# ---------- play_from_extra (collector contract) ----------

def test_play_from_extra_skips_when_inputs_missing():
    # No season_ba / ab_per_game on extra -> guardrail abstains (returns None).
    p = cg.play_from_extra(name="x", market_key="hits_2", sim_prob=0.9, extra={})
    assert p is None


def test_play_from_extra_builds_tb_distribution():
    extra = {
        "season_ba": 0.275,
        "ab_per_game": 3.9,
        "season_ab": 500,
        "season_hr": 20,
        "season_doubles": 30,
        "season_triples": 2,
    }
    p = cg.play_from_extra(name="x", market_key="tb_2", sim_prob=0.6, extra=extra)
    assert p is not None
    assert p.tb_dist is not None
    assert math.isclose(sum(p.tb_dist.values()), 1.0, abs_tol=1e-9)


# ---------- the gate integrated into the builder ----------

def _candidate(market: str, line: str, sim_prob: float, extra: dict) -> MlbPlayCandidate:
    cand = MlbPlayCandidate(
        player_id=f"id-{market}-{line}",
        player_name="Test Player",
        team="NYY",
        opponent="BOS",
        game_id="game",
        market=market,
        line=line,
        stat_value=0.5,
        baseline=0.2,
        trend=0.5,
        matchup=0.5,
        recent_form=0.5,
        extra=extra,
    )
    cand.score = 25.0
    cand.confidence = 60
    cand.tier = "A"
    cand.sim_prob = sim_prob
    return cand


def test_apply_calibration_gate_quarantines_goodman_clone():
    # 2+ Hits at sim 0.94, BA 0.242, 4 AB -> binomial baseline ~0.248, gap ~+0.69 -> FLAG.
    cand = _candidate(
        "Hits", "2+ Hits", sim_prob=0.941,
        extra={"season_ba": 0.242, "ab_per_game": 4.0},
    )
    held = apply_calibration_gate([cand])
    assert len(held) == 1
    assert cand.extra["calibration_status"] == "flag"
    assert cand.extra["held_for_calibration"] is True
    assert not is_publishable_candidate(cand)


def test_apply_calibration_gate_passes_calibrated_play():
    # 1+ Hit at sim 0.73, BA 0.280, 4 AB -> binomial baseline ~0.731, gap ~-0.001 -> ok.
    cand = _candidate(
        "Hits", "1+ Hit", sim_prob=0.730,
        extra={"season_ba": 0.280, "ab_per_game": 4.0},
    )
    held = apply_calibration_gate([cand])
    assert held == []
    assert cand.extra["calibration_status"] == "ok"
    assert "held_for_calibration" not in cand.extra
    assert is_publishable_candidate(cand)


def test_apply_calibration_gate_rbi_soft_warn_never_quarantined():
    # RBI is the weakest baseline (Poisson on context-driven counts) — warn, never drop.
    cand = _candidate(
        "RBI", "2+ RBI", sim_prob=0.95,
        extra={"rbi_per_game": 0.6},  # baseline Poisson(0.6) for >=2 = ~0.12 -> +83pp
    )
    held = apply_calibration_gate([cand])
    assert held == []
    assert cand.extra["calibration_status"] == "warn"
    assert "held_for_calibration" not in cand.extra
    assert is_publishable_candidate(cand)


def test_apply_calibration_gate_unmodeled_market_passes_unchanged():
    cand = _candidate("ML", "Moneyline", sim_prob=0.6, extra={})
    held = apply_calibration_gate([cand])
    assert held == []
    assert "calibration_status" not in cand.extra
    assert is_publishable_candidate(cand)


def test_apply_calibration_gate_missing_inputs_marks_unmodeled():
    # Market is modeled, but the extra payload lacks the inputs -> unmodeled, not dropped.
    cand = _candidate("Hits", "2+ Hits", sim_prob=0.9, extra={})
    held = apply_calibration_gate([cand])
    assert held == []
    assert cand.extra["calibration_status"] == "unmodeled"
    assert is_publishable_candidate(cand)
