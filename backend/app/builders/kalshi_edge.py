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
    PROP_SERIES,
    build_market_lookup,
    build_prop_lookup,
    collect_kalshi_markets,
    normalize_player_name,
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


# --------------------------------------------------- whole-ladder edge join
#
# Backlog #2 (Copper-20+-vs-25+ lesson, systematized): every player with a
# modeled `ladder` ({threshold: prob}) gets EACH rung joined to its Kalshi
# player-prop market (KXWNBAPTS / KXWNBAAST / KXWNBAREB / KXMLBHR, ...) and
# stamped BET / PASS / CHECK independently by the same decision policy as ML
# picks. Purely additive: rows gain `kalshi_ladder`, the board gains
# `ladder_board` (best BET rung per player + the full ladder for display).


def build_ladder_rung(threshold: int, model_prob: float | None, summary: dict[str, Any] | None) -> dict[str, Any]:
    """One rung of a player's ladder, joined (or not) to its Kalshi market."""
    implied_prob = summary.get("implied_prob") if summary else None
    edge_pp = (
        round((model_prob - implied_prob) * 100.0, 1)
        if model_prob is not None and implied_prob is not None
        else None
    )
    return {
        "threshold": threshold,
        "line": f"{threshold}+",
        "model_prob": round(model_prob, 4) if model_prob is not None else None,
        "ticker": summary["ticker"] if summary else None,
        "implied_prob": implied_prob,
        "edge_pp": edge_pp,
        "volume": summary.get("volume", 0) if summary else 0,
        "decision": decide_pick(edge_pp, implied_prob) if summary else "NO_MARKET",
        "model_fair_american": american_odds(model_prob),
        "market_american": american_odds(implied_prob),
    }


def _ladder_entry_rank(entry: dict[str, Any]) -> tuple[bool, float, float]:
    """Sort/dedup rank: has a BET rung, then best BET edge, then best joined edge."""
    best = entry.get("best")
    best_edge = float(best["edge_pp"]) if best and best.get("edge_pp") is not None else float("-inf")
    max_edge = max(
        (float(rung["edge_pp"]) for rung in entry["ladder"] if rung.get("edge_pp") is not None),
        default=float("-inf"),
    )
    return (best is not None, best_edge, max_edge)


def enrich_board_with_ladder(board: dict, *, paths) -> None:
    """Mutate board in place: join every modeled ladder rung to its Kalshi
    player-prop market, stamp each rung, and add ``board["ladder_board"]``.

    Additive and report-only, same failure posture as the ML overlay: a total
    Kalshi failure leaves every rung NO_MARKET and an empty board flagged
    ``available: False``. Sports with no prop series mapping are untouched.
    """
    sport = str(board.get("sport") or "").upper()
    series_by_market = PROP_SERIES.get(sport)
    if not series_by_market:
        return

    date = str(board.get("date") or "")
    lookups: dict[str, dict] = {}
    available = False
    market_count = 0
    for market_key, series in series_by_market.items():
        try:
            markets = collect_kalshi_markets(paths.data_raw, series)
        except Exception:
            markets = None
        if markets is not None:
            available = True
        lookups[market_key] = build_prop_lookup(markets)
        market_count += len(lookups[market_key])

    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for game in board.get("games") or []:
        markets_by_key = game.get("markets") or {}
        for market_key in series_by_market:
            for row in markets_by_key.get(market_key) or []:
                ladder = row.get("ladder")
                if not ladder:
                    continue
                lookup = lookups.get(market_key) or {}
                name_norm = normalize_player_name(row.get("player_name") or "")
                rungs = []
                for threshold in sorted(int(t) for t in ladder):
                    model_prob = ladder.get(threshold, ladder.get(str(threshold)))
                    summary = lookup.get((date, name_norm, threshold))
                    rungs.append(build_ladder_rung(threshold, model_prob, summary))
                row["kalshi_ladder"] = rungs

                if not any(rung["ticker"] for rung in rungs):
                    continue  # no Kalshi ladder for this player today
                bets = [rung for rung in rungs if rung["decision"] == "BET"]
                # Best rung = highest EV per contract among BET stamps
                # (EV/contract = model_prob - implied_prob = edge_pp / 100).
                best = max(bets, key=lambda rung: rung["edge_pp"]) if bets else None
                entry = {
                    "player_id": row.get("player_id"),
                    "player_name": row.get("player_name"),
                    "team": row.get("team"),
                    "opponent": row.get("opponent"),
                    "market": market_key,
                    "game_id": game.get("game_id"),
                    "headline_line": row.get("line"),
                    "best": best,
                    "ladder": rungs,
                    "label": REPORT_LABEL,
                }
                key = (name_norm, market_key)
                current = entries.get(key)
                if current is None or _ladder_entry_rank(entry) > _ladder_entry_rank(current):
                    entries[key] = entry

    players = sorted(entries.values(), key=_ladder_entry_rank, reverse=True)
    board["ladder_board"] = {
        "title": "Whole-Ladder Board",
        "label": REPORT_LABEL,
        "source": "kalshi",
        "series": dict(series_by_market),
        "available": available,
        "market_count": market_count,
        "decision_rules": DECISION_RULES,
        "players": players,
    }
