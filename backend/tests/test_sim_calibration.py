"""Golden calibration tests for the Phase 12 simulation engine.

These verify the engine's contract, not exact magic numbers: determinism,
anchoring to the point estimate under the normal-state mixture, the documented
directional biases (pitcher-K and NBA overs drift down from volume risk),
monotonicity, and boundary behavior.
"""

from __future__ import annotations

from app.models.mlb_model import MlbPlayCandidate
from app.sim.edge import american_to_implied, build_sim_board, edge_pct, sim_tier
from app.sim.sim_engine import SimConfig, sim_prob_pct, simulate_candidate, simulate_candidates

CONFIG = SimConfig()


def make(market: str, stat_value: float, *, line: str = "", extra: dict | None = None) -> MlbPlayCandidate:
    return MlbPlayCandidate(
        player_id=f"{market}-{stat_value}",
        player_name="Fixture",
        team="AAA",
        opponent="BBB",
        game_id="g",
        market=market,
        line=line or f"{market} 1+",
        stat_value=stat_value,
        baseline=0.0,
        trend=0.0,
        matchup=0.0,
        recent_form=0.0,
        extra=extra or {},
    )


def make_hoops(market: str, *, l10: float, l5: float, line: str = "") -> dict:
    """A basketball candidate as the NBA/WNBA builders produce it — a plain dict
    with empirical hit rates and no stat_value."""
    return {
        "player_id": f"{market}-{l10}-{l5}",
        "player_name": "Hooper",
        "team": "AAA",
        "opponent": "BBB",
        "game_id": "g",
        "market": market,
        "line": line or f"{market} 10+",
        "score": 70.0,
        "confidence": 70,
        "tier": "B",
        "reason": "fixture",
        "l10_hit_rate": l10,
        "l5_hit_rate": l5,
    }


def test_deterministic_across_runs():
    candidate = make("Hits", 0.62, line="1+ Hit")
    first = simulate_candidate(candidate, "MLB", CONFIG)
    second = simulate_candidate(candidate, "MLB", CONFIG)
    assert first == second


def test_hr_count_model_anchors_to_per_game_probability():
    # P(>=1 HR) should land near the per-game HR probability, nudged up slightly
    # by the asymmetric hitter state mixture (e.g. position-player pitching).
    candidate = make("HR", 0.30, line="HR 1+", extra={"projected_pa": 4.5})
    prob = simulate_candidate(candidate, "MLB", CONFIG)
    assert 0.26 <= prob <= 0.40


def test_hr_two_plus_is_rarer_than_one_plus():
    one = make("HR", 0.30, line="HR 1+", extra={"projected_pa": 4.5})
    two = make("HR", 0.30, line="HR 2+", extra={"projected_pa": 4.5})
    assert simulate_candidate(two, "MLB", CONFIG) < simulate_candidate(one, "MLB", CONFIG)


def test_hitter_clear_anchors_to_stat_value():
    candidate = make("TB", 0.55, line="2+ TB")
    prob = simulate_candidate(candidate, "MLB", CONFIG)
    assert abs(prob - 0.55) <= 0.05


def test_pitcher_k_overs_drift_down_from_early_hook_risk():
    candidate = make("K", 0.60, line="5+ K")
    prob = simulate_candidate(candidate, "MLB", CONFIG)
    assert prob < 0.60
    assert abs(prob - 0.60 * 0.93) <= 0.05


def test_nba_clear_prob_blends_hit_rates_and_drifts_down():
    # Central clear prob = 0.5*l10 + 0.5*l5 = 0.64; minutes-risk mixture pulls it
    # below that central estimate.
    candidate = make_hoops("PTS", l10=0.62, l5=0.66, line="24+ PTS")
    prob = simulate_candidate(candidate, "NBA", CONFIG)
    assert prob < 0.64
    assert abs(prob - 0.64 * 0.9465) <= 0.05


def test_wnba_reuses_basketball_model():
    candidate = make_hoops("REB", l10=0.70, l5=0.70, line="6+ REB")
    prob = simulate_candidate(candidate, "WNBA", CONFIG)
    assert prob < 0.70  # same minutes-risk drift as NBA


def test_basketball_moneyline_anchors_to_win_pct():
    candidate = make_hoops("ML", l10=0.68, l5=0.68, line="Moneyline")
    prob = simulate_candidate(candidate, "NBA", CONFIG)
    assert abs(prob - 0.68) <= 0.04  # team outcome: noise only, no state mixture


def test_dict_candidate_gets_sim_prob_set_in_place():
    candidate = make_hoops("AST", l10=0.60, l5=0.60, line="6+ AST")
    assert sim_prob_pct(candidate) is None
    simulate_candidates([candidate], "NBA", CONFIG)
    assert "sim_prob" in candidate
    assert sim_prob_pct(candidate) == round(candidate["sim_prob"] * 100, 1)


def test_moneyline_is_noise_only_and_anchors():
    candidate = make("ML", 0.70, line="Moneyline")
    prob = simulate_candidate(candidate, "MLB", CONFIG)
    assert abs(prob - 0.70) <= 0.04


def test_monotonic_in_stat_value():
    low = simulate_candidate(make("TB", 0.30, line="2+ TB"), "MLB", CONFIG)
    mid = simulate_candidate(make("TB", 0.55, line="2+ TB"), "MLB", CONFIG)
    high = simulate_candidate(make("TB", 0.80, line="2+ TB"), "MLB", CONFIG)
    assert low < mid < high


def test_boundaries_stay_in_range():
    near_zero = simulate_candidate(make("Hits", 0.01, line="1+ Hit"), "MLB", CONFIG)
    near_one = simulate_candidate(make("Hits", 0.99, line="1+ Hit"), "MLB", CONFIG)
    assert 0.0 <= near_zero <= 0.10
    assert 0.90 <= near_one <= 1.0


def test_generic_fallback_for_unmodeled_market():
    candidate = make("XYZ", 0.50, line="whatever")
    prob = simulate_candidate(candidate, "SOCCER", CONFIG)
    assert abs(prob - 0.50) <= 0.04


def test_sim_prob_pct_display_and_none():
    candidate = make("Hits", 0.62, line="1+ Hit")
    assert sim_prob_pct(candidate) is None  # not simulated yet
    candidate.sim_prob = 0.6731
    assert sim_prob_pct(candidate) == 67.3


def test_2500_sims_locked():
    assert SimConfig().n_sims == 2500


# --- Sim Edges board: edge + probability tiers (Components 4-5) -------------

def test_american_to_implied():
    assert american_to_implied("-100") == 0.5
    assert abs(american_to_implied("-140") - 0.5833) <= 0.001
    assert abs(american_to_implied("+120") - 0.4545) <= 0.001
    assert american_to_implied(None) is None
    assert american_to_implied("n/a") is None


def test_edge_pct():
    assert edge_pct(0.65, 0.50) == 30.0          # +30%
    assert edge_pct(0.45, 0.50) == -10.0         # negative EV
    assert edge_pct(0.60, None) is None          # no odds -> no edge


def test_sim_tier_thresholds():
    assert sim_tier(0.62, 16.0) == "CORE"
    assert sim_tier(0.56, 11.0) == "STRONG"
    assert sim_tier(0.51, 6.0) == "VALUE"
    assert sim_tier(0.42, 1.0) == "LONGSHOT"
    assert sim_tier(0.62, -2.0) == "HIDDEN"      # negative edge
    assert sim_tier(0.30, 3.0) == "HIDDEN"       # positive edge but prob too low
    assert sim_tier(0.62, None) is None          # no odds -> no tier


def _hoops_row(market, *, sim_prob, odds, tier="B", line="10+", name="P"):
    row = make_hoops(market, l10=sim_prob, l5=sim_prob, line=line)
    row["player_name"] = name
    row["tier"] = tier
    row["sim_prob"] = sim_prob
    if odds is not None:
        row["implied_odds"] = odds
    return row


def test_sim_board_ranks_by_sim_prob_and_excludes_ml_and_pass():
    cands = [
        _hoops_row("PTS", sim_prob=0.64, odds="-100", name="High"),
        _hoops_row("AST", sim_prob=0.52, odds="-120", name="Mid"),
        _hoops_row("ML", sim_prob=0.80, odds="-200", name="Moneyline"),  # excluded (ML)
        _hoops_row("REB", sim_prob=0.99, odds="-100", tier="PASS", name="Passed"),  # excluded (PASS)
    ]
    board = build_sim_board(cands, "NBA")
    names = [p["player_name"] for p in board["players"]]
    assert "Moneyline" not in names and "Passed" not in names
    assert names == ["High", "Mid"]                  # ranked by sim_prob desc
    assert board["players"][0]["sim_prob_pct"] == 64.0
    assert "edge_pct" not in board["players"][0]      # no edge column today
    assert "sim_tier" not in board["players"][0]


def test_sim_board_diversifies_markets_before_filling():
    # Three high Hits + one lower HR: the HR should lead ahead of the 2nd/3rd
    # Hits so one market can't crowd the board.
    cands = [
        make("Hits", 0.94, line="1+ Hit"),
        make("Hits", 0.93, line="2+ Hits"),
        make("Hits", 0.92, line="1+ Hit"),
        make("HR", 0.40, line="HR 1+"),
    ]
    for c in cands:
        c.sim_prob = c.stat_value
        c.tier = "C"
    markets = [p["market"] for p in build_sim_board(cands, "MLB")["players"]]
    assert markets[:2] == ["Hits", "HR"]            # best Hits, then HR before more Hits
    assert markets == ["Hits", "HR", "Hits", "Hits"]


def test_sim_board_mlb_ranks_by_sim_prob():
    cands = [
        make("Hits", 0.40, line="1+ Hit"),
        make("TB", 0.55, line="2+ TB"),
    ]
    for c in cands:
        c.sim_prob = c.stat_value  # stand in for a run
        c.tier = "C"               # PASS-tier is excluded by design
    board = build_sim_board(cands, "MLB")
    assert board["players"][0]["sim_prob_pct"] == 55.0   # TB ranked above Hits
    assert all("edge_pct" not in p for p in board["players"])
