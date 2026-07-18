"""Resolve MLB pick outcomes from the MLB Stats API (the same upstream the
board's collector uses, so player ids and team abbreviations join exactly).

Per date we cache one compact document: every FINAL game with scores plus
per-player batting/pitching counting stats. Prop grading conventions:

- Batter markets (HR / Hits / TB / RBI): graded only if the player batted
  (PA > 0); a pick on a player who didn't play is VOID (excluded), matching
  how a book voids a dnp prop.
- K market: pitcher strikeouts, graded only if the player recorded outs.
- TB = hits + doubles + 2*triples + 3*homeRuns.
- Doubleheaders: the board's game_id can't distinguish games of a DH; the
  player's first final game of that matchup is used (documented limitation).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from backtest.netcache import cache_file, cached_fetch, fetch_json

STATS_API = "https://statsapi.mlb.com/api/v1"


def _today_et() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")

STAT_FOR_MARKET = {"HR": "hr", "Hits": "h", "TB": "tb", "RBI": "rbi", "K": "k"}


def day_results(date: str, *, offline: bool = False) -> dict[str, Any] | None:
    """{"games": [{away, home, away_score, home_score, players: {pid: stats}}]}
    for every FINAL game on the date. Cached; None when unavailable."""
    settled = date < _today_et()  # today's games may not be final yet — don't cache
    cache_key = ("mlb", f"results_{date}.json")
    built = cached_fetch("", cache_key, offline=True)  # cache-only probe
    if built is not None and built.get("_built"):
        return built

    schedule_url = f"{STATS_API}/schedule?sportId=1&date={date}&hydrate=team"
    if settled:
        schedule = cached_fetch(schedule_url, ("mlb", f"schedule_{date}.json"), offline=offline)
    else:
        try:
            schedule = None if offline else fetch_json(schedule_url)
        except Exception:
            schedule = None
    if schedule is None:
        return None
    games_out = []
    for day in schedule.get("dates") or []:
        for game in day.get("games") or []:
            state = (game.get("status") or {}).get("abstractGameState")
            if state != "Final":
                continue
            game_pk = game["gamePk"]
            box = cached_fetch(
                f"{STATS_API}/game/{game_pk}/boxscore",
                ("mlb", f"box_{game_pk}.json"),
                offline=offline,
            )
            if box is None:
                continue
            teams = game.get("teams") or {}
            entry: dict[str, Any] = {
                "gamePk": game_pk,
                "away": ((teams.get("away") or {}).get("team") or {}).get("abbreviation"),
                "home": ((teams.get("home") or {}).get("team") or {}).get("abbreviation"),
                "away_score": (teams.get("away") or {}).get("score"),
                "home_score": (teams.get("home") or {}).get("score"),
                "players": {},
            }
            for side in ("away", "home"):
                box_side = (box.get("teams") or {}).get(side) or {}
                for player in (box_side.get("players") or {}).values():
                    pid = str((player.get("person") or {}).get("id") or "")
                    stats = player.get("stats") or {}
                    bat = stats.get("batting") or {}
                    pit = stats.get("pitching") or {}
                    entry["players"][pid] = {
                        "pa": bat.get("plateAppearances"),
                        "h": bat.get("hits"),
                        "d": bat.get("doubles"),
                        "t": bat.get("triples"),
                        "hr": bat.get("homeRuns"),
                        "rbi": bat.get("rbi"),
                        "k": pit.get("strikeOuts"),
                        "outs": pit.get("outs"),
                    }
            games_out.append(entry)
    built = {"_built": True, "date": date, "games": games_out}
    if settled:
        # persist the compact form so offline replays skip per-game boxscores
        cache_file(*cache_key).write_text(json.dumps(built), encoding="utf-8")
    return built


def _matchup_games(results: dict[str, Any], away: str, home: str) -> list[dict[str, Any]]:
    return [
        g for g in results.get("games") or []
        if g.get("away") == away and g.get("home") == home
    ]


def resolve_prop(pick: dict[str, Any], results: dict[str, Any]) -> int | None:
    """1/0 outcome for a prop pick, or None (void / unresolvable)."""
    stat_key = STAT_FOR_MARKET.get(pick["market"])
    threshold = pick.get("threshold")
    if stat_key is None or threshold is None:
        return None
    pid = pick["player_id"].split("-")[0]
    games = _matchup_games(results, pick_away(pick), pick_home(pick))
    for game in games:
        stats = (game.get("players") or {}).get(pid)
        if stats is None:
            continue
        if stat_key == "k":
            if not stats.get("outs"):
                continue  # didn't pitch in this game
            value = stats.get("k") or 0
        else:
            if not stats.get("pa"):
                continue  # didn't bat — void
            if stat_key == "tb":
                value = (
                    (stats.get("h") or 0)
                    + (stats.get("d") or 0)
                    + 2 * (stats.get("t") or 0)
                    + 3 * (stats.get("hr") or 0)
                )
            else:
                value = stats.get(stat_key) or 0
        return 1 if value >= threshold else 0
    return None


def resolve_moneyline(pick: dict[str, Any], results: dict[str, Any]) -> int | None:
    """1 if the pick's side won the (first final) game of the matchup."""
    games = _matchup_games(results, pick_away(pick), pick_home(pick))
    for game in games:
        away_score, home_score = game.get("away_score"), game.get("home_score")
        if away_score is None or home_score is None or away_score == home_score:
            continue
        winner = game["away"] if away_score > home_score else game["home"]
        return 1 if winner == pick["team"] else 0
    return None


def pick_away(pick: dict[str, Any]) -> str:
    parts = (pick.get("game_id") or "").split("-")
    return parts[0].upper() if len(parts) >= 5 else pick.get("team", "")


def pick_home(pick: dict[str, Any]) -> str:
    parts = (pick.get("game_id") or "").split("-")
    if len(parts) >= 5:
        return parts[1].upper()
    return pick.get("opponent", "")
