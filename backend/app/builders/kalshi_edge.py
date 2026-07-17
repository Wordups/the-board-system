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
- Each ML pick row also gains a ``decision`` field (BET / PASS / CHECK, or
  NO_MARKET when no Kalshi market joins) via ``decide_pick``, and the board
  gains a ``decision_rules`` string spelling out the policy.

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

# ---- decision layer (market-anchored, three outcomes + NO_MARKET) ----
#
# The Kalshi market is the sanity anchor for the sim: a modest model-over-market
# edge is a bet, no edge is a pass, and a *huge* disagreement is treated as a
# probable model error (e.g. the inflated 94% "2+ hits" sims), not free money.
DECISION_MAX_EDGE_PP = 25.0        # above this, suspect the model, not the market
DECISION_IMPLIED_FLOOR = 0.10      # below: longshot territory, stay out
DECISION_IMPLIED_CEILING = 0.80    # above: chalk, no value
DECISION_RULES = (
    f"BET: edge_pp between +{EDGE_THRESHOLD_PP:g} and +{DECISION_MAX_EDGE_PP:g} "
    f"and market implied_prob between {DECISION_IMPLIED_FLOOR:.2f} and {DECISION_IMPLIED_CEILING:.2f}.\n"
    f"PASS: edge_pp < +{EDGE_THRESHOLD_PP:g} (no edge) or implied_prob outside "
    f"{DECISION_IMPLIED_FLOOR:.2f}-{DECISION_IMPLIED_CEILING:.2f} (chalk/longshot).\n"
    f"CHECK: edge_pp > +{DECISION_MAX_EDGE_PP:g} — model disagrees with the market too much; "
    f"probable model error, verify before betting. (No Kalshi market: NO_MARKET.)"
)


def decide_pick(edge_pp: float | None, implied_prob: float | None) -> str:
    """Three-outcome decision for a pick that has a Kalshi market join.

    - "BET":   +5pp <= edge_pp <= +25pp AND 0.10 <= implied_prob <= 0.80
    - "CHECK": edge_pp > +25pp — the model disagrees with the market by so much
      it's probably a model error; the market is the sanity anchor.
    - "PASS":  everything else — edge_pp < +5pp (no edge), implied_prob outside
      the 0.10-0.80 band (chalk/longshot), or the edge can't be computed.
    """
    if edge_pp is None or implied_prob is None:
        return "PASS"
    edge_pp = float(edge_pp)
    implied_prob = float(implied_prob)
    if edge_pp > DECISION_MAX_EDGE_PP:
        return "CHECK"
    if (
        edge_pp >= EDGE_THRESHOLD_PP
        and DECISION_IMPLIED_FLOOR <= implied_prob <= DECISION_IMPLIED_CEILING
    ):
        return "BET"
    return "PASS"


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
                row["decision"] = "NO_MARKET"
                continue
            block = build_kalshi_block(row, summary)
            row["kalshi"] = block
            row["decision"] = decide_pick(block["edge_pp"], block["implied_prob"])
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
                        "decision": row["decision"],
                        "model_fair_american": american_odds(block["model_prob"]),
                        "market_american": american_odds(block["implied_prob"]),
                        "volume": block["volume"],
                        "label": REPORT_LABEL,
                    }
                )

    edge_picks.sort(key=lambda pick: pick["edge_pp"], reverse=True)
    board["decision_rules"] = DECISION_RULES
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
