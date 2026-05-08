"""Lineup / injury awareness shared by NBA and WNBA collectors.

A prop's value depends on who's playing. Drop OUT/DOUBTFUL players from
the candidate pool, flag GTD, and boost remaining players' usage when a
high-usage teammate is OUT (the Maxey-when-Embiid-is-out pattern).
"""

from __future__ import annotations

from typing import Any


# ESPN's injuries field can carry the status under several keys depending
# on whether it came from a roster, summary, or athletes endpoint.
_OUT_TOKENS = ("OUT", "INACTIVE", "INJURED RESERVE", "INJURED-RESERVE", "RULED OUT", " IL ")
_DOUBTFUL_TOKENS = ("DOUBTFUL", "DOUBT")
_GTD_TOKENS = ("QUESTIONABLE", "QUESTION", "DAY-TO-DAY", "DAY TO DAY", "GTD", "PROBABLE")


def _injury_label(injury: dict[str, Any]) -> str:
    return " ".join(
        str(injury.get(key, "") or "")
        for key in ("status", "type", "shortComment", "longComment", "details", "description")
    ).upper()


def extract_injury_status(athlete: dict[str, Any]) -> str:
    """Return one of: ACTIVE, GTD, DOUBTFUL, OUT.

    Defensive — if we can't classify a non-empty injury record we tag it GTD
    (visible warning) rather than ACTIVE (silently treating as healthy).
    """
    injuries = athlete.get("injuries") or []
    if not injuries:
        return "ACTIVE"
    label = _injury_label(injuries[0])
    for token in _OUT_TOKENS:
        if token in label:
            return "OUT"
    for token in _DOUBTFUL_TOKENS:
        if token in label:
            return "DOUBTFUL"
    for token in _GTD_TOKENS:
        if token in label:
            return "GTD"
    return "GTD"


def is_playable(status: str) -> bool:
    return status in {"ACTIVE", "GTD"}


def compute_team_lineup_context(
    profiles: list[dict[str, Any]],
    *,
    star_usage_threshold: float = 0.18,
    max_boost: float = 0.30,
) -> dict[str, Any]:
    """Summarize the team's injury picture into a lineup context dict.

    profiles: list of player profile dicts, each with 'player_name',
        'injury_status', and 'usage_load' (rough offensive load proxy).

    Returns:
        out_players, lost_usage, boost_factor, star_outs (list of names).
    """
    if not profiles:
        return {"out_players": [], "lost_usage": 0.0, "boost_factor": 1.0, "star_outs": []}
    out_players = [p for p in profiles if p.get("injury_status") in {"OUT", "DOUBTFUL"}]
    gtd_players = [p for p in profiles if p.get("injury_status") == "GTD"]
    active_players = [p for p in profiles if is_playable(p.get("injury_status", "ACTIVE"))]
    total_usage = sum(float(p.get("usage_load", 0.0) or 0.0) for p in profiles) or 1.0
    lost_usage = sum(float(p.get("usage_load", 0.0) or 0.0) for p in out_players)
    remaining = sum(float(p.get("usage_load", 0.0) or 0.0) for p in active_players) or 1.0
    boost_factor = 1.0 + min(lost_usage / remaining, max_boost)
    star_outs = [
        p["player_name"]
        for p in out_players
        if float(p.get("usage_load", 0.0) or 0.0) / total_usage >= star_usage_threshold
    ]
    # High-usage GTD teammates create lineup uncertainty — can't boost yet
    # because they may still play, but the user needs to see the risk.
    star_gtd = [
        p["player_name"]
        for p in gtd_players
        if float(p.get("usage_load", 0.0) or 0.0) / total_usage >= star_usage_threshold
    ]
    return {
        "out_players": [
            {"name": p["player_name"], "status": p.get("injury_status")} for p in out_players
        ],
        "lost_usage": round(lost_usage, 2),
        "boost_factor": round(boost_factor, 3),
        "star_outs": star_outs,
        "star_gtd": star_gtd,
    }


def lineup_summary_note(context: dict[str, Any], status: str) -> str:
    """Short human-readable note for the candidate's reason string."""
    parts = [f"Status {status}"]
    if context.get("star_outs"):
        parts.append("Stars OUT: " + ", ".join(context["star_outs"]))
    if context.get("star_gtd"):
        parts.append("Stars GTD: " + ", ".join(context["star_gtd"]))
    if context.get("boost_factor", 1.0) > 1.01:
        parts.append(f"Usage boost {context['boost_factor']:.2f}x")
    return " | ".join(parts)
