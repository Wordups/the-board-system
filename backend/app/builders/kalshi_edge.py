"""Kalshi edge join layer (Phase: market-implied probability overlay).

Joins the board's ML picks to Kalshi game-winner markets and emits a
top-level ``kalshi_edge_board`` report. Purely additive, display-only:

- Each ML pick row gains a ``kalshi`` field:
  ``{ticker, implied_prob, model_prob, edge_pp, volume}`` when a market
  exists for (game date, away, home, pick side), else ``kalshi: null``.
- ``board["kalshi_edge_board"]`` lists picks with ``edge_pp >= +5``, sorted
  desc, with fair American odds for the model probability and market
  American odds for the implied probability. It is labeled
  ``REPORT_ONLY — no execution venue wired`` — nothing here places orders.

Model probability is the pick's existing simulated probability
(``sim_prob_pct / 100``; the board's score IS that probability, so
``score / 100`` is the fallback). Nothing here feeds scoring, sim, or any
existing board field — a total Kalshi failure leaves ``kalshi: null``
everywhere and an empty report.
"""

from __future__ import annotations

from typing import Any

from app.connectors.kalshi_connector import (
    DEFAULT_SERIES,
    build_market_lookup,
    collect_kalshi_markets,
)


EDGE_THRESHOLD_PP = 5.0
REPORT_LABEL = "REPORT_ONLY — no execution venue wired"


def parse_board_game_id(game_id: str) -> tuple[str, str, str] | None:
    """'lad-nyy-2026-07-19' -> ('LAD', 'NYY', '2026-07-19')."""
    parts = (game_id or "").split("-")
    if len(parts) < 5:
        return None
    away, home = parts[0].upper(), parts[1].upper()
    date = "-".join(parts[-3:])
    if not away or not home:
        return None
    return away, home, date


def model_prob_for_row(row: dict[str, Any]) -> float | None:
    """The pick's existing model probability (0..1): sim_prob_pct / 100,
    falling back to score / 100 (the score IS the sim probability)."""
    for field in ("sim_prob_pct", "score"):
        value = row.get(field)
        if value is None:
            continue
        try:
            return min(max(float(value) / 100.0, 0.0), 1.0)
        except (TypeError, ValueError):
            continue
    return None


def american_odds(prob: float | None) -> int | None:
    """Probability (0..1 exclusive) -> American odds. 0.50 -> +100."""
    if prob is None or prob <= 0.0 or prob >= 1.0:
        return None
    if prob > 0.5:
        return -int(round(prob / (1.0 - prob) * 100.0))
    return int(round((1.0 - prob) / prob * 100.0))


def build_kalshi_block(row: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    model_prob = model_prob_for_row(row)
    implied_prob = summary.get("implied_prob")
    edge_pp = (
        round((model_prob - implied_prob) * 100.0, 1)
        if model_prob is not None and implied_prob is not None
        else None
    )
    return {
        "ticker": summary["ticker"],
        "implied_prob": implied_prob,
        "model_prob": round(model_prob, 4) if model_prob is not None else None,
        "edge_pp": edge_pp,
        "volume": summary.get("volume", 0),
    }


def enrich_board_with_kalshi(
    board: dict,
    *,
    paths,
    series_ticker: str = DEFAULT_SERIES,
) -> None:
    """Mutate board in place: annotate ML pick rows with ``kalshi`` and add
    the top-level ``kalshi_edge_board`` report.

    Designed to never break the pipeline — a total Kalshi failure (no fetch,
    no cache) degrades to ``kalshi: null`` on every ML row and an empty
    report flagged ``available: False``.
    """
    try:
        markets = collect_kalshi_markets(paths.data_raw, series_ticker)
    except Exception:
        markets = None
    lookup = build_market_lookup(markets)

    edge_picks: list[dict[str, Any]] = []
    for game in board.get("games") or []:
        parsed = parse_board_game_id(game.get("game_id") or "")
        markets_by_key = game.get("markets") or {}
        for row in markets_by_key.get("ML") or []:
            summary = None
            if parsed is not None:
                away, home, date = parsed
                side = str(row.get("team") or "").upper()
                summary = lookup.get((date, away, home, side))
            if summary is None:
                row["kalshi"] = None
                continue
            block = build_kalshi_block(row, summary)
            row["kalshi"] = block
            if block["edge_pp"] is not None and block["edge_pp"] >= EDGE_THRESHOLD_PP:
                edge_picks.append(
                    {
                        "player_name": row.get("player_name"),
                        "team": row.get("team"),
                        "opponent": row.get("opponent"),
                        "game_id": game.get("game_id"),
                        "line": row.get("line"),
                        "ticker": block["ticker"],
                        "model_prob": block["model_prob"],
                        "implied_prob": block["implied_prob"],
                        "edge_pp": block["edge_pp"],
                        "model_fair_american": american_odds(block["model_prob"]),
                        "market_american": american_odds(block["implied_prob"]),
                        "volume": block["volume"],
                        "label": REPORT_LABEL,
                    }
                )

    edge_picks.sort(key=lambda pick: pick["edge_pp"], reverse=True)
    board["kalshi_edge_board"] = {
        "title": "Kalshi Edge Report",
        "label": REPORT_LABEL,
        "source": "kalshi",
        "series_ticker": series_ticker,
        "min_edge_pp": EDGE_THRESHOLD_PP,
        "available": markets is not None,
        "market_count": len(lookup),
        "picks": edge_picks,
    }
