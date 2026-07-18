"""Resolve WNBA/NBA pick outcomes from ESPN game summaries.

The basketball boards are built FROM ESPN (rosters, schedules), so the
board's ``game_id`` IS the ESPN event id and ``player_id`` IS the ESPN
athlete id — the join is exact, no name matching.

Stat labels come from the boxscore's own ``labels`` list (MIN, PTS, FG,
3PT, ...). 3PM is the made-count of the "3PT" made-attempted pair. A
player with no minutes (DNP) voids the prop.
"""

from __future__ import annotations

from typing import Any

from backtest.netcache import cached_fetch

ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/{league}/summary?event={event}"
LEAGUE_PATH = {"wnba": "wnba", "nba": "nba"}

LABEL_FOR_MARKET = {"PTS": "PTS", "REB": "REB", "AST": "AST", "3PM": "3PT"}


def event_summary(sport: str, event_id: str, *, offline: bool = False) -> dict[str, Any] | None:
    league = LEAGUE_PATH.get(sport)
    if not league or not event_id.isdigit():
        return None
    return cached_fetch(
        ESPN_SUMMARY.format(league=league, event=event_id),
        (sport, f"summary_{event_id}.json"),
        offline=offline,
    )


def _final_competition(summary: dict[str, Any]) -> dict[str, Any] | None:
    try:
        comp = summary["header"]["competitions"][0]
    except (KeyError, IndexError, TypeError):
        return None
    status = (((comp.get("status") or {}).get("type")) or {}).get("name")
    return comp if status == "STATUS_FINAL" else None


def _player_stat(summary: dict[str, Any], player_id: str, label: str) -> tuple[int | None, bool]:
    """(value, played). value None when the player is absent/DNP."""
    for team in (summary.get("boxscore") or {}).get("players") or []:
        for block in team.get("statistics") or []:
            labels = block.get("labels") or block.get("names") or []
            if label not in labels:
                continue
            idx = labels.index(label)
            min_idx = labels.index("MIN") if "MIN" in labels else None
            for entry in block.get("athletes") or []:
                if str((entry.get("athlete") or {}).get("id")) != player_id:
                    continue
                stats = entry.get("stats") or []
                if entry.get("didNotPlay") or len(stats) <= idx:
                    return None, False
                if min_idx is not None and min_idx < len(stats):
                    try:
                        if float(str(stats[min_idx]).replace("+", "")) <= 0:
                            return None, False
                    except ValueError:
                        return None, False
                raw = str(stats[idx])
                try:
                    value = int(raw.split("-")[0]) if label == "3PT" else int(float(raw))
                except ValueError:
                    return None, False
                return value, True
    return None, False


def resolve_prop(pick: dict[str, Any], summary: dict[str, Any]) -> int | None:
    if _final_competition(summary) is None:
        return None
    label = LABEL_FOR_MARKET.get(pick["market"])
    threshold = pick.get("threshold")
    if label is None or threshold is None:
        return None
    value, played = _player_stat(summary, pick["player_id"].split("-")[0], label)
    if not played or value is None:
        return None
    return 1 if value >= threshold else 0


def resolve_moneyline(pick: dict[str, Any], summary: dict[str, Any]) -> int | None:
    comp = _final_competition(summary)
    if comp is None:
        return None
    side = pick.get("team", "").upper()
    for competitor in comp.get("competitors") or []:
        abbrev = str((competitor.get("team") or {}).get("abbreviation") or "").upper()
        if abbrev == side:
            winner = competitor.get("winner")
            return None if winner is None else int(bool(winner))
    return None
