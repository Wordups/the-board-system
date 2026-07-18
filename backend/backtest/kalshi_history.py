"""Historical Kalshi data for the decision-layer replay (MLB moneylines).

Two sources, both cached for offline replay:

1. SETTLED MARKETS — ``GET /markets?series_ticker=KXMLBGAME&status=settled``
   windowed by close timestamp. Gives every finalized game-winner market's
   ticker + result (yes/no), keyed (date, away, home, side) via the repo's
   own ticker parser. Kalshi's MLB series has settled history from
   ~2026-05-19 onward.

2. CANDLESTICKS — ``GET /series/.../markets/<ticker>/candlesticks`` at
   60-minute periods. The candle at/just before the board snapshot's commit
   time recovers the PREGAME market price, which is what the BET/PASS/CHECK
   decision layer needed. When the market hadn't traded yet by snapshot
   time, the first candle within the next few hours is used and flagged.
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.connectors.kalshi_connector import parse_game_ticker

from backtest.netcache import cache_file, cached_fetch, fetch_json

KALSHI_API = "https://external-api.kalshi.com/trade-api/v2"
SERIES = "KXMLBGAME"
PAGE_LIMIT = 200
MAX_PAGES = 10
POST_SNAPSHOT_GRACE_HOURS = 6


def _date_ts(date: str) -> int:
    return calendar.timegm(datetime.strptime(date, "%Y-%m-%d").timetuple())


def settled_day(date: str, *, offline: bool = False) -> list[dict[str, Any]] | None:
    """All settled KXMLBGAME markets whose close falls on this (ET) game date.

    Window: 16:00 UTC game day -> 12:00 UTC next day (games close at final
    out, ET afternoon through late night).
    """
    key = ("kalshi", f"settled_{date}.json")
    cached = cached_fetch("", key, offline=True)
    if isinstance(cached, list):
        return cached
    if offline:
        return None
    min_ts = _date_ts(date) + 16 * 3600
    max_ts = _date_ts(date) + 36 * 3600
    markets: list[dict[str, Any]] = []
    cursor = ""
    try:
        for _ in range(MAX_PAGES):
            url = (
                f"{KALSHI_API}/markets?series_ticker={SERIES}&status=settled"
                f"&limit={PAGE_LIMIT}&min_close_ts={min_ts}&max_close_ts={max_ts}"
            )
            if cursor:
                url += f"&cursor={cursor}"
            payload = fetch_json(url)
            markets.extend(payload.get("markets") or [])
            cursor = payload.get("cursor") or ""
            if not cursor:
                break
    except Exception:
        return None
    compact = [
        {"ticker": m.get("ticker"), "result": m.get("result")}
        for m in markets
        if m.get("result") in ("yes", "no")
    ]
    cache_file(*key).write_text(json.dumps(compact), encoding="utf-8")
    return compact


def settled_lookup(dates: list[str], *, offline: bool = False) -> dict[tuple, dict[str, Any]]:
    """(date, away, home, side) -> {ticker, won} across the requested dates."""
    lookup: dict[tuple, dict[str, Any]] = {}
    for date in dates:
        for market in settled_day(date, offline=offline) or []:
            parsed = parse_game_ticker(market.get("ticker") or "")
            if parsed is None:
                continue
            key = (parsed["date"], parsed["away"], parsed["home"], parsed["side"])
            if key in lookup:
                continue  # first (game 1) market of a doubleheader wins
            lookup[key] = {"ticker": market["ticker"], "won": market["result"] == "yes"}
    return lookup


def _candle_prob(candle: dict[str, Any]) -> float | None:
    def dollars(block: dict | None, field: str = "close_dollars") -> float | None:
        try:
            return float((block or {}).get(field))
        except (TypeError, ValueError):
            return None

    bid = dollars(candle.get("yes_bid"))
    ask = dollars(candle.get("yes_ask"))
    if bid is not None and ask is not None and bid <= ask and not (bid <= 0.0 and ask >= 1.0):
        return round((bid + ask) / 2.0, 4)
    last = dollars(candle.get("price"))
    if last is not None and last > 0.0:
        return round(last, 4)
    return None


def pregame_price(ticker: str, snapshot_ts: int, *, offline: bool = False) -> dict[str, Any] | None:
    """Market-implied YES prob at (or just before) the snapshot timestamp.

    Falls forward up to POST_SNAPSHOT_GRACE_HOURS when the book only opened
    after the snapshot; the result is then flagged ``post_snapshot``.
    """
    hour_bucket = snapshot_ts // 3600
    payload = cached_fetch(
        f"{KALSHI_API}/series/{SERIES}/markets/{ticker}/candlesticks"
        f"?start_ts={snapshot_ts - 12 * 3600}"
        f"&end_ts={snapshot_ts + POST_SNAPSHOT_GRACE_HOURS * 3600}&period_interval=60",
        ("kalshi", "candles", f"{ticker}_{hour_bucket}.json"),
        offline=offline,
    )
    if payload is None:
        return None
    candles = payload.get("candlesticks") or []
    before = [c for c in candles if int(c.get("end_period_ts", 0)) <= snapshot_ts]
    pool = before[-1:] if before else candles[:1]
    for candle in pool:
        prob = _candle_prob(candle)
        if prob is not None:
            return {
                "implied_prob": prob,
                "ts": int(candle.get("end_period_ts", 0)),
                "post_snapshot": not before,
            }
    return None
