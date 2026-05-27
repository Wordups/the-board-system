"""Edge + probability tiers for the Sim Edges board (Phase 12, Components 4-5).

The live value-pricing feature already attaches American `implied_odds` to NBA /
WNBA rows, which unblocks the edge computation Option B had deferred:

    book_implied = implied prob from the American odds (raw, includes vig)
    edge_pct     = (sim_prob - book_implied) / book_implied   [as a percentage]

Tiers are the original Phase 12 probability tiers (CORE/STRONG/VALUE/LONGSHOT),
not the deterministic A/B/C. Rows with no odds (MLB today) get edge_pct=None and
sim_tier=None — the board ranks those by sim_prob until MLB odds land.
"""

from __future__ import annotations

import re

from app.sim.outcome_models import get_field


def american_to_implied(odds) -> float | None:
    """American odds (e.g. '-140', '+120', '-100') -> implied probability (with vig)."""
    if odds is None:
        return None
    match = re.search(r"[-+]?\d+", str(odds))
    if not match:
        return None
    value = int(match.group())
    if value == 0:
        return None
    if value < 0:
        return (-value) / (-value + 100.0)
    return 100.0 / (value + 100.0)


def edge_pct(sim_prob: float, implied: float | None) -> float | None:
    """Percentage edge of the simulated probability over the book-implied probability."""
    if implied is None or implied <= 0:
        return None
    return round((sim_prob - implied) / implied * 100.0, 1)


def sim_tier(sim_prob: float, edge: float | None) -> str | None:
    """Probability-based tier. None when there is no edge (no odds) to tier on."""
    if edge is None:
        return None
    if edge < 0:
        return "HIDDEN"  # negative EV
    if edge >= 15 and sim_prob >= 0.60:
        return "CORE"
    if edge >= 10 and sim_prob >= 0.55:
        return "STRONG"
    if edge >= 5 and sim_prob >= 0.50:
        return "VALUE"
    if edge >= 0 and sim_prob >= 0.40:
        return "LONGSHOT"
    return "HIDDEN"  # non-negative edge but below the probability floor


def build_sim_board(candidates, sport: str, limit: int = 10) -> dict:
    """Rank simulated player-markets by simulated probability.

    Sim-% only: the board does not show edge. The only "odds" in the pipeline today
    are model-derived (``hit_rate_to_implied_odds``), not a sportsbook, so a true
    edge vs the house waits on real-odds ingestion (the reserved ``book_odds`` field
    / Phase 14). The ``edge_pct`` / ``sim_tier`` helpers above are kept ready and
    tested for that day. Excludes moneylines and PASS-tier candidates.
    """
    rows = []
    for candidate in candidates:
        sim_prob = get_field(candidate, "sim_prob", None)
        if sim_prob is None:
            continue
        if get_field(candidate, "market", "") == "ML":
            continue
        if get_field(candidate, "tier", "") == "PASS":
            continue
        rows.append(
            {
                "player_id": str(get_field(candidate, "player_id", "")),
                "player_name": get_field(candidate, "player_name", ""),
                "team": get_field(candidate, "team", ""),
                "opponent": get_field(candidate, "opponent", ""),
                "market": get_field(candidate, "market", ""),
                "line": get_field(candidate, "line", ""),
                "sim_prob_pct": round(float(sim_prob) * 100.0, 1),
            }
        )

    rows.sort(key=lambda row: row["sim_prob_pct"], reverse=True)
    # Diversify: lead with the best play from each market, then fill by sim_prob,
    # so a single high-probability market (e.g. MLB Hits) can't crowd the board.
    primary, overflow, seen = [], [], set()
    for row in rows:
        if row["market"] in seen:
            overflow.append(row)
        else:
            seen.add(row["market"])
            primary.append(row)
    ordered = primary + overflow
    return {"title": "Sim Top 10", "market": "SIM", "players": ordered[:limit]}
