"""Whole-ladder quoting tests (backlog #2 — Copper-20+-vs-25+ systematized).

Covers the three layers:
1. Ladder math from synthetic distributions — monotone non-increasing rungs,
   exact agreement with the single-line sim probability at the headline rung.
2. Kalshi prop-market parsing (KXWNBAPTS / KXMLBHR ticker anatomy) and the
   (date, player, threshold) lookup with doubleheader dedup.
3. The edge join: each rung joined to its own market and stamped
   BET / PASS / CHECK / NO_MARKET independently; `ladder_board` carries the
   best BET rung per player plus the full ladder; players dedup'd.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.builders.kalshi_edge import enrich_board_with_ladder
from app.connectors.kalshi_connector import (
    build_prop_lookup,
    normalize_player_name,
    parse_prop_ticker,
    summarize_prop_market,
)
from app.models.mlb_model import MlbPlayCandidate
from app.sim.outcome_models import LADDER_RUNGS
from app.sim.sim_engine import (
    SimConfig,
    simulate_candidate,
    simulate_candidate_ladder,
    simulate_candidates,
)

CONFIG = SimConfig()


# ---------------------------------------------------------------- fixtures


def make_hr(stat_value: float = 0.30, *, line: str = "HR 1+") -> MlbPlayCandidate:
    return MlbPlayCandidate(
        player_id="hr-1",
        player_name="Slugger",
        team="AAA",
        opponent="BBB",
        game_id="g",
        market="HR",
        line=line,
        stat_value=stat_value,
        baseline=0.0,
        trend=0.0,
        matchup=0.0,
        recent_form=0.0,
        extra={"projected_pa": 4.5},
    )


def make_hoops(market: str, *, line: str, l10: float = 0.55, l5: float = 0.55, name: str = "Hooper") -> dict:
    return {
        "player_id": f"{market}-{name}",
        "player_name": name,
        "team": "AAA",
        "opponent": "BBB",
        "game_id": "g",
        "market": market,
        "line": line,
        "score": 70.0,
        "confidence": 70,
        "tier": "B",
        "reason": "fixture",
        "l10_hit_rate": l10,
        "l5_hit_rate": l5,
    }


def prop_market(
    ticker: str,
    sub_title: str,
    *,
    implied: float | None = 0.45,
    volume: str = "100.00",
) -> dict:
    price = None if implied is None else f"{implied:.4f}"
    return {
        "ticker": ticker,
        "yes_bid": None,
        "yes_ask": None,
        "last_price": None,
        "volume": None,
        "yes_bid_dollars": price,
        "yes_ask_dollars": price,
        "last_price_dollars": price,
        "volume_fp": volume,
        "yes_sub_title": sub_title,
        "title": sub_title + " points",
    }


def patch_markets(monkeypatch, by_series: dict[str, list[dict] | None]) -> None:
    """Route the edge join's Kalshi fetch to canned per-series markets (no
    network, no cache). Unlisted series behave as a total Kalshi failure."""
    monkeypatch.setattr(
        "app.builders.kalshi_edge.collect_kalshi_markets",
        lambda data_raw_dir, series_ticker: by_series.get(series_ticker),
    )


class FakePaths:
    def __init__(self, data_raw: Path):
        self.data_raw = data_raw


# ------------------------------------------------------------- ladder math


def test_hr_ladder_monotone_and_matches_headline_sim():
    candidate = make_hr(line="HR 1+")
    prob = simulate_candidate(candidate, "MLB", CONFIG)
    ladder = simulate_candidate_ladder(candidate, "MLB", CONFIG)
    assert set(ladder) == set(LADDER_RUNGS["HR"])
    assert ladder[1] == prob  # exact — same seed, same sample paths
    assert ladder[2] < ladder[1]
    assert all(0.0 <= p <= 1.0 for p in ladder.values())


def test_hr_ladder_matches_sim_at_two_plus_headline():
    candidate = make_hr(line="HR 2+")
    prob = simulate_candidate(candidate, "MLB", CONFIG)
    ladder = simulate_candidate_ladder(candidate, "MLB", CONFIG)
    assert ladder[2] == prob


def test_basketball_pts_ladder_monotone_and_matches_headline_sim():
    candidate = make_hoops("PTS", line="20+ PTS")
    prob = simulate_candidate(candidate, "WNBA", CONFIG)
    ladder = simulate_candidate_ladder(candidate, "WNBA", CONFIG)
    assert set(ladder) == set(LADDER_RUNGS["PTS"])  # headline 20 is a rung
    assert ladder[20] == prob  # exact replay of the headline sim
    thresholds = sorted(ladder)
    probs = [ladder[t] for t in thresholds]
    assert probs == sorted(probs, reverse=True)  # survival fn: non-increasing
    assert ladder[10] > ladder[20] > ladder[30]


def test_basketball_offline_headline_added_to_rungs():
    candidate = make_hoops("PTS", line="18+ PTS")
    ladder = simulate_candidate_ladder(candidate, "WNBA", CONFIG)
    assert set(ladder) == set(LADDER_RUNGS["PTS"]) | {18}
    prob = simulate_candidate(candidate, "WNBA", CONFIG)
    assert ladder[18] == prob
    assert ladder[15] >= ladder[18] >= ladder[20]


def test_ast_and_reb_ladders_use_market_rungs():
    ast = simulate_candidate_ladder(make_hoops("AST", line="4+ AST"), "WNBA", CONFIG)
    reb = simulate_candidate_ladder(make_hoops("REB", line="8+ REB"), "NBA", CONFIG)
    assert set(ast) == set(LADDER_RUNGS["AST"])
    assert set(reb) == set(LADDER_RUNGS["REB"])


def test_ladder_deterministic_across_runs():
    candidate = make_hoops("PTS", line="15+ PTS")
    assert simulate_candidate_ladder(candidate, "WNBA", CONFIG) == simulate_candidate_ladder(
        candidate, "WNBA", CONFIG
    )


def test_unladdered_markets_get_no_ladder():
    assert simulate_candidate_ladder(make_hoops("3PM", line="2+ 3PM"), "WNBA", CONFIG) is None
    assert simulate_candidate_ladder(make_hoops("ML", line="Moneyline"), "WNBA", CONFIG) is None
    hits = make_hr()
    hits.market = "Hits"
    hits.line = "1+ Hit"
    assert simulate_candidate_ladder(hits, "MLB", CONFIG) is None


def test_simulate_candidates_attaches_ladders():
    hoops = make_hoops("PTS", line="20+ PTS")
    hr = make_hr()
    simulate_candidates([hoops], sport="WNBA")
    simulate_candidates([hr], sport="MLB")
    assert isinstance(hoops["ladder"], dict) and 20 in hoops["ladder"]
    assert isinstance(hr.ladder, dict) and set(hr.ladder) == {1, 2}


# ------------------------------------------------------ prop ticker parsing


def test_parse_prop_ticker_wnba():
    parsed = parse_prop_ticker("KXWNBAPTS-26JUL18WSHGS-WSHSCITRON22-20")
    assert parsed == {"date": "2026-07-18", "threshold": 20}


def test_parse_prop_ticker_mlb_hr_with_time_block():
    parsed = parse_prop_ticker("KXMLBHR-26JUL181510CINCOL-CINEDELACRUZ44-2")
    assert parsed == {"date": "2026-07-18", "threshold": 2}


def test_parse_prop_ticker_rejects_game_and_junk_tickers():
    assert parse_prop_ticker("KXMLBGAME-26JUL191920LADNYY-NYY") is None
    assert parse_prop_ticker("KXWNBAPTS-26JUL18WSHGS-WSHSCITRON22-XX") is None
    assert parse_prop_ticker("") is None


def test_normalize_player_name_folds_diacritics_and_punctuation():
    assert normalize_player_name("José Ramírez") == normalize_player_name("Jose Ramirez")
    assert normalize_player_name("  A'ja  Wilson ") == "a ja wilson"


def test_summarize_prop_market_reads_player_from_sub_title():
    market = prop_market("KXWNBAPTS-26JUL18WSHGS-WSHSCITRON22-20", "Sonia Citron: 20+", implied=0.24)
    summary = summarize_prop_market(market)
    assert summary["player"] == "Sonia Citron"
    assert summary["threshold"] == 20
    assert summary["date"] == "2026-07-18"
    assert summary["implied_prob"] == 0.24


def test_build_prop_lookup_dedups_doubleheader_by_rank():
    low = prop_market("KXWNBAPTS-26JUL18AAABBB-AAAPLAYER1-20", "Star Player: 20+", implied=0.40, volume="5.00")
    high = prop_market("KXWNBAPTS-26JUL18AAABBB2-AAAPLAYER1-20", "Star Player: 20+", implied=0.44, volume="500.00")
    lookup = build_prop_lookup([low, high])
    assert len(lookup) == 1
    assert lookup[("2026-07-18", "star player", 20)]["implied_prob"] == 0.44


# ------------------------------------------------------- edge join + board


def ladder_board_fixture() -> dict:
    """WNBA board: Copper with a full PTS ladder, a CHECK-edge player, and a
    doubleheader duplicate of Copper in a second game."""
    copper = {
        "player_id": "1",
        "player_name": "Kahleah Copper",
        "team": "PHX",
        "opponent": "CHI",
        "line": "20+ PTS",
        "score": 55.0,
        "sim_prob_pct": 55.0,
        "ladder": {10: 0.95, 15: 0.80, 20: 0.55, 25: 0.30, 30: 0.12},
    }
    star = {
        "player_id": "2",
        "player_name": "Test Star",
        "team": "LVA",
        "opponent": "SEA",
        "line": "15+ PTS",
        "score": 85.0,
        "sim_prob_pct": 85.0,
        "ladder": {10: 0.95, 15: 0.85, 20: 0.50, 25: 0.20, 30: 0.05},
    }
    return {
        "sport": "WNBA",
        "date": "2026-07-18",
        "games": [
            {
                "game_id": "401000001",
                "matchup": "PHX @ CHI",
                "markets": {"PTS": [copper], "AST": [], "REB": []},
            },
            {
                "game_id": "401000002",
                "matchup": "LVA @ SEA",
                "markets": {"PTS": [star, dict(copper)], "AST": [], "REB": []},
            },
        ],
    }


def copper_markets() -> list[dict]:
    def m(threshold: int, implied: float) -> dict:
        return prop_market(
            f"KXWNBAPTS-26JUL18PHXCHI-PHXKCOPPER2-{threshold}",
            f"Kahleah Copper: {threshold}+",
            implied=implied,
        )

    return [
        m(10, 0.93),  # chalk: implied above the 0.80 ceiling -> PASS
        m(15, 0.72),  # edge +8.0 -> BET
        m(20, 0.44),  # edge +11.0 -> BET (best)
        m(25, 0.28),  # edge +2.0 -> PASS
        # no 30+ market -> NO_MARKET
    ]


def star_markets() -> list[dict]:
    return [
        prop_market(
            "KXWNBAPTS-26JUL18LVASEA-LVATSTAR0-15",
            "Test Star: 15+",
            implied=0.40,  # edge +45 -> CHECK (model error territory)
        )
    ]


def test_each_rung_joined_and_stamped_independently(monkeypatch, tmp_path):
    patch_markets(monkeypatch, {"KXWNBAPTS": copper_markets() + star_markets()})
    board = ladder_board_fixture()
    enrich_board_with_ladder(board, paths=FakePaths(tmp_path))

    row = board["games"][0]["markets"]["PTS"][0]
    rungs = {rung["threshold"]: rung for rung in row["kalshi_ladder"]}
    assert set(rungs) == {10, 15, 20, 25, 30}
    assert rungs[10]["decision"] == "PASS"  # chalk
    assert rungs[15]["decision"] == "BET" and rungs[15]["edge_pp"] == 8.0
    assert rungs[20]["decision"] == "BET" and rungs[20]["edge_pp"] == 11.0
    assert rungs[25]["decision"] == "PASS"
    assert rungs[30]["decision"] == "NO_MARKET" and rungs[30]["ticker"] is None
    assert rungs[20]["model_prob"] == 0.55
    assert rungs[20]["implied_prob"] == 0.44

    star_rungs = {r["threshold"]: r for r in board["games"][1]["markets"]["PTS"][0]["kalshi_ladder"]}
    assert star_rungs[15]["decision"] == "CHECK"


def test_ladder_board_best_rung_and_dedup(monkeypatch, tmp_path):
    patch_markets(monkeypatch, {"KXWNBAPTS": copper_markets() + star_markets()})
    board = ladder_board_fixture()
    enrich_board_with_ladder(board, paths=FakePaths(tmp_path))

    ladder_board = board["ladder_board"]
    assert ladder_board["available"] is True
    names = [entry["player_name"] for entry in ladder_board["players"]]
    assert names.count("Kahleah Copper") == 1  # doubleheader duplicate dedup'd
    assert set(names) == {"Kahleah Copper", "Test Star"}

    copper_entry = next(e for e in ladder_board["players"] if e["player_name"] == "Kahleah Copper")
    # Best BET rung is the highest-EV rung — 20+, NOT the 15+ headline-adjacent
    # rung and NOT the model's favorite 10+ (chalk).
    assert copper_entry["best"]["threshold"] == 20
    assert copper_entry["best"]["decision"] == "BET"
    assert len(copper_entry["ladder"]) == 5  # full ladder kept for display

    star_entry = next(e for e in ladder_board["players"] if e["player_name"] == "Test Star")
    assert star_entry["best"] is None  # CHECK is not a BET
    # BET-carrying entries sort ahead of CHECK/PASS-only entries.
    assert names[0] == "Kahleah Copper"


def test_ladder_join_degrades_to_no_market_when_kalshi_down(monkeypatch, tmp_path):
    patch_markets(monkeypatch, {})  # every series: total Kalshi failure
    board = ladder_board_fixture()
    enrich_board_with_ladder(board, paths=FakePaths(tmp_path))
    ladder_board = board["ladder_board"]
    assert ladder_board["available"] is False
    assert ladder_board["players"] == []
    row = board["games"][0]["markets"]["PTS"][0]
    assert all(r["decision"] == "NO_MARKET" for r in row["kalshi_ladder"])


def test_sports_without_prop_series_are_untouched():
    board = {"sport": "SOCCER", "date": "2026-07-18", "games": []}
    enrich_board_with_ladder(board, paths=FakePaths(Path("unused")))
    assert "ladder_board" not in board


def test_rows_without_ladder_are_skipped(monkeypatch, tmp_path):
    patch_markets(monkeypatch, {"KXWNBAPTS": copper_markets()})
    board = ladder_board_fixture()
    del board["games"][0]["markets"]["PTS"][0]["ladder"]
    enrich_board_with_ladder(board, paths=FakePaths(tmp_path))
    assert "kalshi_ladder" not in board["games"][0]["markets"]["PTS"][0]
