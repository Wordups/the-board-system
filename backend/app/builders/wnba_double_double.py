"""WNBA Double-Double Watch.

A view over the already-scored WNBA candidates — no new sim or scoring. It joins
each player's projected PTS / REB / AST across their per-market rows (the model's
projection is carried in each candidate's `reason` as "Proj X.X") and surfaces
the players closest to a double-double (>=10 in two categories).

This is the signature WNBA prop angle (double-doubles price attractively and hit
often for high-usage bigs/guards). Each entry names the two categories, the
projected values, and how far the binding category is from 10.
"""
from __future__ import annotations

import re

DD_THRESHOLD = 10.0       # a "double" = >= 10 in a category
DD_FLIRT_WITHIN = 4.0     # binding category within this of 10 => on the watch
DD_CATEGORIES = ("PTS", "REB", "AST")
_PROJ_RE = re.compile(r"Proj\s+([0-9.]+)")
_TIER_ORDER = {"A": 3, "B": 2, "C": 1}


def _projection(reason: str) -> float | None:
    m = _PROJ_RE.search(reason or "")
    return float(m.group(1)) if m else None


def _best_tier(*tiers: str | None) -> str | None:
    present = [t for t in tiers if t]
    return max(present, key=lambda t: _TIER_ORDER.get(t, 0)) if present else None


def build_double_double_watch(candidates: list[dict], limit: int = 8) -> dict:
    # Collect each player's best projection per DD category.
    players: dict[str, dict] = {}
    for c in candidates:
        market = c.get("market")
        if market not in DD_CATEGORIES:
            continue
        proj = _projection(c.get("reason", ""))
        if proj is None:
            continue
        pid = str(c.get("player_id"))
        entry = players.setdefault(pid, {
            "player_id": pid, "player_name": c.get("player_name"),
            "team": c.get("team"), "opponent": c.get("opponent"),
            "cats": {}, "tiers": {},
        })
        if market not in entry["cats"] or proj > entry["cats"][market]:
            entry["cats"][market] = proj
            entry["tiers"][market] = c.get("tier")

    watch = []
    for entry in players.values():
        cats = entry["cats"]
        if len(cats) < 2:
            continue
        (m1, v1), (m2, v2) = sorted(cats.items(), key=lambda kv: kv[1], reverse=True)[:2]
        binding_market, binding_val = (m2, v2)  # the weaker of the top two = DD constraint
        if binding_val < DD_THRESHOLD - DD_FLIRT_WITHIN:
            continue
        projected_dd = v1 >= DD_THRESHOLD and v2 >= DD_THRESHOLD
        needed = round(max(0.0, DD_THRESHOLD - binding_val), 1)
        if projected_dd:
            needed_label = "Projected double-double"
        else:
            needed_label = f"{needed:.1f} {binding_market} from a double-double"
        watch.append({
            "player_id": entry["player_id"],
            "player_name": entry["player_name"],
            "team": entry["team"],
            "opponent": entry["opponent"],
            "combo": f"{m1}+{m2}",
            "primary": {"market": m1, "proj": round(v1, 1), "tier": entry["tiers"].get(m1)},
            "secondary": {"market": m2, "proj": round(v2, 1), "tier": entry["tiers"].get(m2)},
            "needed": needed,
            "needed_label": needed_label,
            "projected_dd": projected_dd,
            "tier": _best_tier(entry["tiers"].get(m1), entry["tiers"].get(m2)),
        })

    # Projected double-doubles first, then closest-to-DD, then biggest combined line.
    watch.sort(key=lambda w: (
        0 if w["projected_dd"] else 1,
        w["needed"],
        -(w["primary"]["proj"] + w["secondary"]["proj"]),
    ))
    return {
        "title": "Double-Double Watch",
        "subtitle": "Closest to a double-double (>=10 in two categories)",
        "threshold": DD_THRESHOLD,
        "flirt_within": DD_FLIRT_WITHIN,
        "players": watch[:limit],
    }
