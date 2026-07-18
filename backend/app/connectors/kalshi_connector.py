"""Kalshi public market-data connector (read-only, NO auth).

Fetches open game-winner markets for a Kalshi series (default MLB:
``KXMLBGAME``; pass e.g. an NBA/NFL series ticker for other sports), parses
tickers into ``{date, away, home, side}``, normalizes team codes to the
repo's MLB StatsAPI abbreviations (the codes the collectors/builders emit:
AZ, ATH, CWS, KC, SD, SF, TB, WSH, ...), and derives a market-implied win
probability per side.

API reality, verified against the live endpoint (2026-07-17):
- Public host ``https://external-api.kalshi.com/trade-api/v2`` serves market
  data with no authentication.
- On this host prices arrive as *dollar strings* (``yes_bid_dollars``,
  ``yes_ask_dollars``, ``last_price_dollars`` — e.g. ``"0.4500"``); the
  documented integer-cent fields (``yes_bid``/``yes_ask``/``last_price``)
  come back null. Both shapes are handled (dollars preferred, cents/100 as
  fallback).
- Volume arrives as ``volume_fp`` (decimal string); integer ``volume`` is
  null. Both are handled.
- An unquoted book shows bid 0.00 / ask 1.00 (sentinel-only) — treated as
  empty: fall back to last price, else ``implied_prob = None``.
- Event blocks look like ``26JUL191920LADNYY`` (YYMONDD + HHMM ET +
  away+home team codes concatenated) with an optional doubleheader suffix
  (``...TBBOSG2``). Kalshi side codes match StatsAPI abbreviations exactly
  today; an alias map still normalizes legacy codes (OAK, ARI, CHW, WAS...)
  defensively.
- Pagination is cursor-based (``cursor`` request param / response field).

Failure posture: a total API failure NEVER raises out of this module —
``collect_kalshi_markets`` serves a stale cache if one exists, else returns
``None``. A board build must never crash because Kalshi had a bad moment.

Raw responses are cached under the repo's raw data dir
(``backend/data_raw/kalshi_<series>_raw.json``) with a 15-minute TTL so
repeated builds don't hammer the API.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from app.outputs.json_writer import write_json


KALSHI_API_BASE = "https://external-api.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT = 10
PAGE_LIMIT = 200
MAX_PAGES = 20  # hard stop so a cursor loop can never spin forever
CACHE_TTL_SECONDS = 15 * 60

DEFAULT_SERIES = "KXMLBGAME"
SPORT_SERIES: dict[str, str] = {
    "MLB": "KXMLBGAME",
    "NBA": "KXNBAGAME",
    "NFL": "KXNFLGAME",
}

# Player-prop ladder series per sport/market (whole-ladder quoting). Verified
# live 2026-07-18: tickers look like
#   KXWNBAPTS-26JUL18WSHGS-WSHSCITRON22-20   (Sonia Citron: 20+ points)
#   KXMLBHR-26JUL181510CINCOL-CINEDELACRUZ44-2 (Elly De La Cruz: 2+ home runs)
# i.e. SERIES-EVENT-PLAYERTAG-THRESHOLD, with the integer rung as the final
# segment and the player's display name in title/yes_sub_title before ':'.
PROP_SERIES: dict[str, dict[str, str]] = {
    "WNBA": {"PTS": "KXWNBAPTS", "AST": "KXWNBAAST", "REB": "KXWNBAREB"},
    "NBA": {"PTS": "KXNBAPTS", "AST": "KXNBAAST", "REB": "KXNBAREB"},
    "MLB": {"HR": "KXMLBHR"},
}

# Kalshi/legacy code -> repo canonical (MLB StatsAPI) abbreviation. Kalshi's
# MLB side codes currently match StatsAPI exactly; these aliases only absorb
# legacy/alternate spellings so a Kalshi-side rename can't silently break the
# join.
TEAM_ALIASES: dict[str, str] = {
    "ANA": "LAA",
    "ARI": "AZ",
    "CHW": "CWS",
    "FLA": "MIA",
    "KCR": "KC",
    "OAK": "ATH",
    "SDP": "SD",
    "SFG": "SF",
    "TBR": "TB",
    "WAS": "WSH",
    "WSN": "WSH",
}

CANONICAL_MLB_CODES: frozenset[str] = frozenset(
    {
        "ATH", "ATL", "AZ", "BAL", "BOS", "CHC", "CIN", "CLE", "COL", "CWS",
        "DET", "HOU", "KC", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY",
        "PHI", "PIT", "SD", "SEA", "SF", "STL", "TB", "TEX", "TOR", "WSH",
    }
)

KNOWN_TEAM_CODES: frozenset[str] = frozenset(CANONICAL_MLB_CODES | set(TEAM_ALIASES))

MONTHS: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# KXMLBGAME event block: YYMONDD + HHMM + AWAYHOME [+ G<n> doubleheader tag].
EVENT_PATTERN = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d+)([A-Z]+?)(G\d+)?$")


def normalize_team_code(code: str) -> str:
    code = (code or "").strip().upper()
    return TEAM_ALIASES.get(code, code)


def split_team_concat(concat: str, side: str) -> tuple[str, str] | None:
    """Split an away+home concat (e.g. 'LADNYY', 'WSHATH') into (away, home).

    Team codes are 2-5 chars, so the split point is ambiguous by length alone.
    Try every split where BOTH halves are known codes; when several splits are
    valid, the market's side code (which must be one of the two teams) breaks
    the tie. Unknown codes fall back to an affix match against the side code.
    """
    side = (side or "").upper()
    candidates = [
        (concat[:i], concat[i:])
        for i in range(2, len(concat) - 1)
        if concat[:i] in KNOWN_TEAM_CODES and concat[i:] in KNOWN_TEAM_CODES
    ]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        sided = [pair for pair in candidates if side in pair]
        if len(sided) == 1:
            return sided[0]
        return candidates[0]
    # Unknown code(s): the side code is still one of the two teams.
    if side and len(concat) > len(side):
        if concat.endswith(side):
            return concat[: -len(side)], side
        if concat.startswith(side):
            return side, concat[len(side):]
    return None


def parse_game_ticker(ticker: str) -> dict[str, str] | None:
    """Parse e.g. 'KXMLBGAME-26JUL191920LADNYY-NYY' into
    {'date': '2026-07-19', 'away': 'LAD', 'home': 'NYY', 'side': 'NYY'}.

    Team codes are normalized to the repo's StatsAPI abbreviations. Returns
    None for any ticker that doesn't look like a game-winner market.
    """
    parts = (ticker or "").split("-")
    if len(parts) != 3:
        return None
    _series, event, raw_side = parts
    match = EVENT_PATTERN.match(event)
    if not match:
        return None
    year_2d, month_name, day, _time_hhmm, team_concat, _dh_tag = match.groups()
    month = MONTHS.get(month_name)
    if month is None:
        return None
    day_int = int(day)
    if not 1 <= day_int <= 31:
        return None

    side = normalize_team_code(raw_side)
    split = split_team_concat(team_concat, raw_side.upper())
    if split is None:
        return None
    away, home = (normalize_team_code(code) for code in split)
    if side not in (away, home):
        return None

    return {
        "date": f"{2000 + int(year_2d):04d}-{month:02d}-{day_int:02d}",
        "away": away,
        "home": home,
        "side": side,
    }


def _parse_price(market: dict[str, Any], field: str) -> float | None:
    """Price as probability (0..1). Prefers the dollar-string field this host
    actually populates; falls back to the documented integer-cent field."""
    dollars = market.get(f"{field}_dollars")
    if dollars not in (None, ""):
        try:
            return min(max(float(dollars), 0.0), 1.0)
        except (TypeError, ValueError):
            pass
    cents = market.get(field)
    if cents in (None, ""):
        return None
    try:
        return min(max(float(cents) / 100.0, 0.0), 1.0)
    except (TypeError, ValueError):
        return None


def implied_probability(market: dict[str, Any]) -> float | None:
    """Market-implied YES probability: mid of yes_bid/yes_ask; falls back to
    last price when the book is unquoted; None when there is no price at all.

    A bid 0.00 / ask 1.00 book is Kalshi's empty-book sentinel, not a real
    quote — it goes to the last-price fallback.
    """
    bid = _parse_price(market, "yes_bid")
    ask = _parse_price(market, "yes_ask")
    if bid is not None and ask is not None and bid <= ask and not (bid <= 0.0 and ask >= 1.0):
        return round((bid + ask) / 2.0, 4)
    last = _parse_price(market, "last_price")
    if last is not None and last > 0.0:
        return round(last, 4)
    return None


def market_volume(market: dict[str, Any]) -> int:
    for field in ("volume_fp", "volume"):
        value = market.get(field)
        if value in (None, ""):
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return 0


def summarize_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """Ticker + parsed game fields + implied probability + volume, or None
    when the ticker isn't a parseable game-winner market."""
    parsed = parse_game_ticker(market.get("ticker") or "")
    if parsed is None:
        return None
    return {
        "ticker": market["ticker"],
        **parsed,
        "implied_prob": implied_probability(market),
        "volume": market_volume(market),
    }


def build_market_lookup(
    markets: list[dict[str, Any]] | None,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """(date, away, home, side) -> market summary.

    Doubleheaders produce two markets with the same key (the board's game_id
    can't distinguish games of a doubleheader either); the priced,
    higher-volume market wins.
    """
    lookup: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for market in markets or []:
        summary = summarize_market(market)
        if summary is None:
            continue
        key = (summary["date"], summary["away"], summary["home"], summary["side"])
        current = lookup.get(key)
        if current is None or _lookup_rank(summary) > _lookup_rank(current):
            lookup[key] = summary
    return lookup


def _lookup_rank(summary: dict[str, Any]) -> tuple[int, int]:
    return (int(summary["implied_prob"] is not None), summary["volume"])


# ------------------------------------------------------- player-prop ladders

# Leading YYMONDD of a prop event block ('26JUL18WSHGS', '26JUL181510CINCOL').
PROP_EVENT_DATE_PATTERN = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})")


def normalize_player_name(name: str) -> str:
    """Join key for player names: ASCII-folded, lowercased, alnum+space only.

    Handles diacritics (Jose Ramirez vs José Ramírez) and punctuation so the
    board's collector names join Kalshi's display names reliably.
    """
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return " ".join(text.split())


def parse_prop_ticker(ticker: str) -> dict[str, Any] | None:
    """'KXWNBAPTS-26JUL18WSHGS-WSHSCITRON22-20' ->
    {'date': '2026-07-18', 'threshold': 20}. None for non-prop tickers."""
    parts = (ticker or "").split("-")
    if len(parts) != 4:
        return None
    _series, event, _player_tag, raw_threshold = parts
    match = PROP_EVENT_DATE_PATTERN.match(event)
    if not match:
        return None
    year_2d, month_name, day = match.groups()
    month = MONTHS.get(month_name)
    if month is None:
        return None
    day_int = int(day)
    if not 1 <= day_int <= 31:
        return None
    try:
        threshold = int(raw_threshold)
    except ValueError:
        return None
    if threshold <= 0:
        return None
    return {
        "date": f"{2000 + int(year_2d):04d}-{month:02d}-{day_int:02d}",
        "threshold": threshold,
    }


def prop_player_name(market: dict[str, Any]) -> str:
    """Player display name from 'Sonia Citron: 20+ points' style titles."""
    for field in ("yes_sub_title", "title"):
        value = market.get(field)
        if value and ":" in str(value):
            return str(value).split(":", 1)[0].strip()
    return ""


def summarize_prop_market(market: dict[str, Any]) -> dict[str, Any] | None:
    """Ticker + date + player + rung threshold + implied probability + volume,
    or None when the ticker isn't a parseable player-prop ladder market."""
    parsed = parse_prop_ticker(market.get("ticker") or "")
    if parsed is None:
        return None
    player = prop_player_name(market)
    if not player:
        return None
    return {
        "ticker": market["ticker"],
        "player": player,
        **parsed,
        "implied_prob": implied_probability(market),
        "volume": market_volume(market),
    }


def build_prop_lookup(
    markets: list[dict[str, Any]] | None,
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """(date, normalized player name, threshold) -> prop market summary.

    Doubleheaders can quote the same player/rung twice on a date; the priced,
    higher-volume market wins (same rank rule as the game-winner lookup).
    """
    lookup: dict[tuple[str, str, int], dict[str, Any]] = {}
    for market in markets or []:
        summary = summarize_prop_market(market)
        if summary is None:
            continue
        key = (summary["date"], normalize_player_name(summary["player"]), summary["threshold"])
        current = lookup.get(key)
        if current is None or _lookup_rank(summary) > _lookup_rank(current):
            lookup[key] = summary
    return lookup


def fetch_json(url: str, *, timeout: int = REQUEST_TIMEOUT) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "the-board-system/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_open_markets(
    series_ticker: str = DEFAULT_SERIES,
    *,
    timeout: int = REQUEST_TIMEOUT,
) -> list[dict[str, Any]] | None:
    """All open markets for a series, following pagination cursors.

    Returns None on ANY failure (network, HTTP, bad JSON) — callers treat
    None as "Kalshi unavailable", never as an error to propagate.
    """
    markets: list[dict[str, Any]] = []
    cursor = ""
    try:
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "limit": PAGE_LIMIT,
                "status": "open",
                "series_ticker": series_ticker,
            }
            if cursor:
                params["cursor"] = cursor
            payload = fetch_json(
                f"{KALSHI_API_BASE}/markets?{urllib.parse.urlencode(params)}",
                timeout=timeout,
            )
            markets.extend(payload.get("markets") or [])
            cursor = payload.get("cursor") or ""
            if not cursor:
                break
    except Exception:
        return None
    return markets


def cache_path_for(data_raw_dir: Path, series_ticker: str) -> Path:
    return Path(data_raw_dir) / f"kalshi_{series_ticker.lower()}_raw.json"


def _read_cache(cache_path: Path) -> dict[str, Any] | None:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        return None
    return payload


def collect_kalshi_markets(
    data_raw_dir: Path,
    series_ticker: str = DEFAULT_SERIES,
    *,
    ttl_seconds: int = CACHE_TTL_SECONDS,
    now: float | None = None,
) -> list[dict[str, Any]] | None:
    """Open markets for a series, served from the raw-data cache when fresh.

    - Cache fresh (< ttl): no network call at all.
    - Cache stale/missing: fetch + rewrite cache.
    - Fetch fails: serve the stale cache if one exists, else None.
    """
    now = time.time() if now is None else now
    cache_path = cache_path_for(data_raw_dir, series_ticker)
    cached = _read_cache(cache_path)
    if cached is not None:
        try:
            fetched_at = float(cached.get("fetched_at", 0.0))
        except (TypeError, ValueError):
            fetched_at = 0.0
        if 0.0 <= now - fetched_at < ttl_seconds:
            return cached["markets"]

    markets = fetch_open_markets(series_ticker)
    if markets is None:
        return cached["markets"] if cached is not None else None

    write_json(
        cache_path,
        {
            "series_ticker": series_ticker,
            "fetched_at": now,
            "markets": markets,
        },
    )
    return markets
