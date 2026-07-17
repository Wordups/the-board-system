from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.builders.kalshi_edge import (
    EDGE_THRESHOLD_PP,
    REPORT_LABEL,
    american_odds,
    enrich_board_with_kalshi,
    model_prob_for_row,
    parse_board_game_id,
)
from app.connectors import kalshi_connector
from app.connectors.kalshi_connector import (
    build_market_lookup,
    collect_kalshi_markets,
    implied_probability,
    market_volume,
    parse_game_ticker,
    summarize_market,
)


# ---------------------------------------------------------------- helpers


def kalshi_market(
    ticker: str,
    *,
    yes_bid: str | None = "0.4500",
    yes_ask: str | None = "0.4800",
    last_price: str | None = "0.4800",
    volume_fp: str | None = "263.06",
) -> dict:
    """A market shaped like the live external-api host: dollar-string prices,
    null integer-cent fields, volume in volume_fp."""
    return {
        "ticker": ticker,
        "yes_bid": None,
        "yes_ask": None,
        "last_price": None,
        "volume": None,
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "last_price_dollars": last_price,
        "volume_fp": volume_fp,
    }


def board_with_ml_row(*, game_id: str, team: str, opponent: str, sim_prob_pct: float) -> dict:
    return {
        "sport": "MLB",
        "games": [
            {
                "game_id": game_id,
                "matchup": f"{team} @ {opponent}",
                "markets": {
                    "ML": [
                        {
                            "player_id": "147-moneyline",
                            "player_name": "Some Team",
                            "team": team,
                            "opponent": opponent,
                            "line": "Moneyline",
                            "score": sim_prob_pct,
                            "sim_prob_pct": sim_prob_pct,
                        }
                    ],
                    "HR": [],
                },
            }
        ],
    }


class FakePaths:
    def __init__(self, data_raw: Path):
        self.data_raw = data_raw


# ---------------------------------------------------------- ticker parsing


def test_parse_game_ticker_basic():
    parsed = parse_game_ticker("KXMLBGAME-26JUL191920LADNYY-NYY")
    assert parsed == {"date": "2026-07-19", "away": "LAD", "home": "NYY", "side": "NYY"}


def test_parse_game_ticker_doubleheader_suffix():
    parsed = parse_game_ticker("KXMLBGAME-26JUL171910TBBOSG2-TB")
    assert parsed == {"date": "2026-07-17", "away": "TB", "home": "BOS", "side": "TB"}


def test_parse_game_ticker_short_and_long_codes_split():
    # 2-char + 3-char concat (SF + SEA) and 3-char + 3-char (WSH + ATH)
    assert parse_game_ticker("KXMLBGAME-26JUL191610SFSEA-SF")["away"] == "SF"
    assert parse_game_ticker("KXMLBGAME-26JUL191610SFSEA-SEA")["home"] == "SEA"
    parsed = parse_game_ticker("KXMLBGAME-26JUL191605WSHATH-ATH")
    assert (parsed["away"], parsed["home"]) == ("WSH", "ATH")


def test_parse_game_ticker_normalizes_legacy_team_codes():
    parsed = parse_game_ticker("KXMLBGAME-26JUL192010CHWOAK-OAK")
    assert parsed == {"date": "2026-07-19", "away": "CWS", "home": "ATH", "side": "ATH"}
    parsed = parse_game_ticker("KXMLBGAME-26JUL192140STLARI-ARI")
    assert (parsed["home"], parsed["side"]) == ("AZ", "AZ")


def test_parse_game_ticker_unknown_code_falls_back_to_side_affix():
    # 'XXQ' is not a known code; the side suffix still anchors the split.
    parsed = parse_game_ticker("KXMLBGAME-26JUL191920XXQNYY-NYY")
    assert parsed == {"date": "2026-07-19", "away": "XXQ", "home": "NYY", "side": "NYY"}


def test_parse_game_ticker_rejects_garbage():
    assert parse_game_ticker("") is None
    assert parse_game_ticker("KXMLBGAME") is None
    assert parse_game_ticker("KXMLBGAME-NOTANEVENT-NYY") is None
    assert parse_game_ticker("KXMLBGAME-26XYZ191920LADNYY-NYY") is None  # bad month
    assert parse_game_ticker("KXMLBGAME-26JUL191920LADNYY-SEA") is None  # side not in game
    assert parse_game_ticker("KXHIGHNY-26JUL19-B58.5") is None  # non-game series shape


# ---------------------------------------------------------------- pricing


def test_implied_probability_uses_mid_of_dollar_quotes():
    market = kalshi_market("KXMLBGAME-26JUL191920LADNYY-NYY", yes_bid="0.4500", yes_ask="0.4800")
    assert implied_probability(market) == 0.465


def test_implied_probability_falls_back_to_integer_cent_fields():
    market = {"ticker": "T", "yes_bid": 45, "yes_ask": 48, "last_price": 46, "volume": 10}
    assert implied_probability(market) == 0.465


def test_implied_probability_empty_book_sentinel_uses_last_price():
    market = kalshi_market("T", yes_bid="0.0000", yes_ask="1.0000", last_price="0.4800")
    assert implied_probability(market) == 0.48


def test_implied_probability_none_when_no_prices_at_all():
    market = kalshi_market("T", yes_bid=None, yes_ask=None, last_price=None)
    assert implied_probability(market) is None
    market_zero_last = kalshi_market("T", yes_bid="0.0000", yes_ask="1.0000", last_price="0.0000")
    assert implied_probability(market_zero_last) is None


def test_market_volume_prefers_volume_fp_then_int_then_zero():
    assert market_volume(kalshi_market("T", volume_fp="263.06")) == 263
    assert market_volume({"volume": 41}) == 41
    assert market_volume({"volume": None, "volume_fp": None}) == 0


def test_summarize_market_carries_parsed_fields_and_price():
    summary = summarize_market(kalshi_market("KXMLBGAME-26JUL191920LADNYY-NYY"))
    assert summary["ticker"] == "KXMLBGAME-26JUL191920LADNYY-NYY"
    assert summary["date"] == "2026-07-19"
    assert summary["side"] == "NYY"
    assert summary["implied_prob"] == 0.465
    assert summary["volume"] == 263


def test_build_market_lookup_prefers_priced_higher_volume_doubleheader_leg():
    unpriced_g1 = kalshi_market(
        "KXMLBGAME-26JUL171310TBBOS-TB", yes_bid=None, yes_ask=None, last_price=None, volume_fp="900"
    )
    priced_g2 = kalshi_market("KXMLBGAME-26JUL171910TBBOSG2-TB", volume_fp="120")
    lookup = build_market_lookup([unpriced_g1, priced_g2])
    assert lookup[("2026-07-17", "TB", "BOS", "TB")]["ticker"].endswith("G2-TB")


# ------------------------------------------------------------- edge math


def test_model_prob_prefers_sim_prob_then_score():
    assert model_prob_for_row({"sim_prob_pct": 62.0, "score": 10.0}) == 0.62
    assert model_prob_for_row({"sim_prob_pct": None, "score": 55.5}) == 0.555
    assert model_prob_for_row({}) is None


def test_american_odds_conversion():
    assert american_odds(0.62) == -163
    assert american_odds(0.4) == 150
    assert american_odds(0.5) == 100
    assert american_odds(None) is None
    assert american_odds(0.0) is None
    assert american_odds(1.0) is None


def test_parse_board_game_id():
    assert parse_board_game_id("lad-nyy-2026-07-19") == ("LAD", "NYY", "2026-07-19")
    assert parse_board_game_id("bad") is None


# ------------------------------------------------------------- join layer


def test_enrich_board_attaches_kalshi_block_and_edge_report(monkeypatch, tmp_path):
    board = board_with_ml_row(
        game_id="lad-nyy-2026-07-19", team="NYY", opponent="LAD", sim_prob_pct=62.0
    )
    markets = [
        kalshi_market("KXMLBGAME-26JUL191920LADNYY-NYY", yes_bid="0.4500", yes_ask="0.4800"),
        kalshi_market("KXMLBGAME-26JUL191920LADNYY-LAD", yes_bid="0.5200", yes_ask="0.5500"),
    ]
    monkeypatch.setattr(
        "app.builders.kalshi_edge.collect_kalshi_markets",
        lambda data_raw_dir, series_ticker: markets,
    )

    enrich_board_with_kalshi(board, paths=FakePaths(tmp_path))

    row = board["games"][0]["markets"]["ML"][0]
    assert row["kalshi"] == {
        "ticker": "KXMLBGAME-26JUL191920LADNYY-NYY",
        "implied_prob": 0.465,
        "model_prob": 0.62,
        "edge_pp": 15.5,
        "volume": 263,
    }

    report = board["kalshi_edge_board"]
    assert report["label"] == REPORT_LABEL
    assert report["available"] is True
    assert report["min_edge_pp"] == EDGE_THRESHOLD_PP
    assert len(report["picks"]) == 1
    pick = report["picks"][0]
    assert pick["edge_pp"] == 15.5
    assert pick["model_fair_american"] == -163
    assert pick["market_american"] == 115  # 0.465 implied
    assert pick["label"] == REPORT_LABEL


def test_enrich_board_below_threshold_gets_block_but_no_report_pick(monkeypatch, tmp_path):
    board = board_with_ml_row(
        game_id="lad-nyy-2026-07-19", team="NYY", opponent="LAD", sim_prob_pct=48.0
    )
    markets = [kalshi_market("KXMLBGAME-26JUL191920LADNYY-NYY")]
    monkeypatch.setattr(
        "app.builders.kalshi_edge.collect_kalshi_markets",
        lambda data_raw_dir, series_ticker: markets,
    )

    enrich_board_with_kalshi(board, paths=FakePaths(tmp_path))

    row = board["games"][0]["markets"]["ML"][0]
    assert row["kalshi"]["edge_pp"] == 1.5
    assert board["kalshi_edge_board"]["picks"] == []


def test_enrich_board_no_market_for_game_sets_kalshi_null(monkeypatch, tmp_path):
    board = board_with_ml_row(
        game_id="sd-kc-2026-07-19", team="KC", opponent="SD", sim_prob_pct=55.0
    )
    markets = [kalshi_market("KXMLBGAME-26JUL191920LADNYY-NYY")]
    monkeypatch.setattr(
        "app.builders.kalshi_edge.collect_kalshi_markets",
        lambda data_raw_dir, series_ticker: markets,
    )

    enrich_board_with_kalshi(board, paths=FakePaths(tmp_path))

    assert board["games"][0]["markets"]["ML"][0]["kalshi"] is None


def test_enrich_board_unquoted_market_yields_null_edge_not_crash(monkeypatch, tmp_path):
    board = board_with_ml_row(
        game_id="lad-nyy-2026-07-19", team="NYY", opponent="LAD", sim_prob_pct=62.0
    )
    markets = [
        kalshi_market(
            "KXMLBGAME-26JUL191920LADNYY-NYY", yes_bid=None, yes_ask=None, last_price=None
        )
    ]
    monkeypatch.setattr(
        "app.builders.kalshi_edge.collect_kalshi_markets",
        lambda data_raw_dir, series_ticker: markets,
    )

    enrich_board_with_kalshi(board, paths=FakePaths(tmp_path))

    row = board["games"][0]["markets"]["ML"][0]
    assert row["kalshi"]["implied_prob"] is None
    assert row["kalshi"]["edge_pp"] is None
    assert board["kalshi_edge_board"]["picks"] == []


def test_enrich_board_total_api_failure_never_crashes(monkeypatch, tmp_path):
    board = board_with_ml_row(
        game_id="lad-nyy-2026-07-19", team="NYY", opponent="LAD", sim_prob_pct=62.0
    )
    monkeypatch.setattr(
        "app.builders.kalshi_edge.collect_kalshi_markets",
        lambda data_raw_dir, series_ticker: None,
    )

    enrich_board_with_kalshi(board, paths=FakePaths(tmp_path))

    assert board["games"][0]["markets"]["ML"][0]["kalshi"] is None
    report = board["kalshi_edge_board"]
    assert report["available"] is False
    assert report["picks"] == []


def test_enrich_board_does_not_touch_existing_fields(monkeypatch, tmp_path):
    board = board_with_ml_row(
        game_id="lad-nyy-2026-07-19", team="NYY", opponent="LAD", sim_prob_pct=62.0
    )
    before = json.loads(json.dumps(board))
    markets = [kalshi_market("KXMLBGAME-26JUL191920LADNYY-NYY")]
    monkeypatch.setattr(
        "app.builders.kalshi_edge.collect_kalshi_markets",
        lambda data_raw_dir, series_ticker: markets,
    )

    enrich_board_with_kalshi(board, paths=FakePaths(tmp_path))

    row = board["games"][0]["markets"]["ML"][0]
    for key, value in before["games"][0]["markets"]["ML"][0].items():
        assert row[key] == value  # additive only: every pre-existing field intact
    assert board["games"][0]["markets"]["HR"] == []
    assert board["sport"] == "MLB"


# ------------------------------------------------------------ cache / TTL


def test_collect_kalshi_markets_uses_fresh_cache_without_network(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_fetch(series_ticker):
        calls["n"] += 1
        return [kalshi_market("KXMLBGAME-26JUL191920LADNYY-NYY")]

    monkeypatch.setattr(kalshi_connector, "fetch_open_markets", fake_fetch)

    first = collect_kalshi_markets(tmp_path, "KXMLBGAME", now=1000.0)
    second = collect_kalshi_markets(tmp_path, "KXMLBGAME", now=1000.0 + 60)
    assert calls["n"] == 1
    assert first == second
    assert (tmp_path / "kalshi_kxmlbgame_raw.json").exists()


def test_collect_kalshi_markets_refetches_after_ttl(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_fetch(series_ticker):
        calls["n"] += 1
        return [kalshi_market(f"KXMLBGAME-26JUL191920LADNYY-NYY", volume_fp=str(calls["n"]))]

    monkeypatch.setattr(kalshi_connector, "fetch_open_markets", fake_fetch)

    collect_kalshi_markets(tmp_path, "KXMLBGAME", now=1000.0)
    stale = collect_kalshi_markets(tmp_path, "KXMLBGAME", now=1000.0 + 15 * 60 + 1)
    assert calls["n"] == 2
    assert stale[0]["volume_fp"] == "2"


def test_collect_kalshi_markets_serves_stale_cache_when_fetch_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        kalshi_connector,
        "fetch_open_markets",
        lambda series_ticker: [kalshi_market("KXMLBGAME-26JUL191920LADNYY-NYY")],
    )
    collect_kalshi_markets(tmp_path, "KXMLBGAME", now=1000.0)

    monkeypatch.setattr(kalshi_connector, "fetch_open_markets", lambda series_ticker: None)
    stale = collect_kalshi_markets(tmp_path, "KXMLBGAME", now=1000.0 + 3600)
    assert stale is not None
    assert stale[0]["ticker"] == "KXMLBGAME-26JUL191920LADNYY-NYY"


def test_collect_kalshi_markets_total_failure_no_cache_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(kalshi_connector, "fetch_open_markets", lambda series_ticker: None)
    assert collect_kalshi_markets(tmp_path, "KXMLBGAME", now=1000.0) is None


def test_fetch_open_markets_follows_cursor_pagination(monkeypatch):
    pages = {
        "": {"markets": [kalshi_market("KXMLBGAME-26JUL191920LADNYY-NYY")], "cursor": "abc"},
        "abc": {"markets": [kalshi_market("KXMLBGAME-26JUL191920LADNYY-LAD")], "cursor": ""},
    }
    seen_urls = []

    def fake_fetch_json(url, *, timeout):
        seen_urls.append(url)
        cursor = ""
        if "cursor=" in url:
            cursor = url.split("cursor=")[1].split("&")[0]
        return pages[cursor]

    monkeypatch.setattr(kalshi_connector, "fetch_json", fake_fetch_json)

    markets = kalshi_connector.fetch_open_markets("KXMLBGAME")
    assert len(markets) == 2
    assert len(seen_urls) == 2
    assert "series_ticker=KXMLBGAME" in seen_urls[0]
    assert "cursor=abc" in seen_urls[1]


def test_fetch_open_markets_returns_none_on_network_error(monkeypatch):
    def boom(url, *, timeout):
        raise OSError("network down")

    monkeypatch.setattr(kalshi_connector, "fetch_json", boom)
    assert kalshi_connector.fetch_open_markets("KXMLBGAME") is None
