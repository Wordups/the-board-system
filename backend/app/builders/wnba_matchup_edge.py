"""WNBA Matchup Edge.

Surfaces the team-level defense-vs-market edge the model already computes but
keeps buried in the score. Each candidate's reason carries "<MKT> matchup R.RRx"
where R = opponent-allowed / league-baseline for that market. That ratio is the
same for every player on a team in a given market, so the useful unit is the
*matchup* (which defense bleeds which market) plus the best player to target it.

A view over already-scored candidates — no new sim/scoring. Team-level today;
per-position DvP (split opponent-allowed by the scorer's position) is the bigger
follow-up that needs a collector change.
"""
from __future__ import annotations

import re

MATCHUP_EDGE_MIN = 1.08  # opponent allows >= 8% above league avg in this market
_RATIO_RE = re.compile(r"matchup ([0-9.]+)x")
_TIER_ORDER = {"A": 3, "B": 2, "C": 1}


def _ratio(reason: str) -> float | None:
    m = _RATIO_RE.search(reason or "")
    return float(m.group(1)) if m else None


def build_matchup_edge(candidates: list[dict], limit: int = 10) -> dict:
    # Group by (attacking team, opponent, market); the ratio is shared across the
    # group, so we keep the single best player to target the matchup.
    groups: dict[tuple, dict] = {}
    for c in candidates:
        market = c.get("market")
        if not market or market == "ML":
            continue
        ratio = _ratio(c.get("reason", ""))
        if ratio is None or ratio < MATCHUP_EDGE_MIN:
            continue
        key = (c.get("team"), c.get("opponent"), market)
        rank = (_TIER_ORDER.get(c.get("tier"), 0), c.get("score") or 0)
        group = groups.get(key)
        if group is None:
            groups[key] = {"ratio": ratio, "team": c.get("team"),
                           "opponent": c.get("opponent"), "market": market,
                           "best": c, "best_rank": rank}
        elif rank > group["best_rank"]:
            group["best"], group["best_rank"] = c, rank

    rows = []
    for g in groups.values():
        c = g["best"]
        sim = c.get("sim_prob")
        rows.append({
            "market": g["market"],
            "opponent": g["opponent"],
            "attack_team": g["team"],
            "matchup_ratio": round(g["ratio"], 2),
            "matchup_label": f"{g['opponent']} allow +{round((g['ratio'] - 1) * 100)}% {g['market']}",
            "target": {
                "player_id": str(c.get("player_id")),
                "player_name": c.get("player_name"),
                "team": c.get("team"),
                "line": c.get("line"),
                "tier": c.get("tier"),
                "score": c.get("score"),
                "sim_prob_pct": round(sim * 100, 1) if sim is not None else None,
            },
        })

    rows.sort(key=lambda x: (x["matchup_ratio"], x["target"]["score"] or 0), reverse=True)
    return {
        "title": "Matchup Edge",
        "subtitle": "Defenses bleeding a market — and who to target",
        "min_ratio": MATCHUP_EDGE_MIN,
        "matchups": rows[:limit],
    }
