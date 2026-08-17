"""NFL collector — ESPN public feeds for the weekly slate.

Two consumers, two different collection depths:

* **Pure predictions** need the *current* season's game logs for every skill
  player on the upcoming slate, plus each opponent defense's recent
  allowances. That is the same shape the NBA collector produces.
* **Salary categories (historical)** need a *three-season rolling* log window,
  but only for players who appear in the contracts file — the salary board's
  universe is exactly "players we know the money for". Bounding the history
  pull that way keeps the hourly CI run inside its budget: the contracts file
  is dozens of players, not the ~1,600 on 32 rosters.

Football is weekly, not nightly, so the "slate" here is every game inside the
next eight days rather than a single date.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import json
import re
from typing import Any, Iterable

import requests

from app.outputs.json_writer import write_json
from app.scoring.lineups import extract_injury_status
from app.utils.dates import now_et, today_et


ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_TEAM_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/roster"
ESPN_TEAM_SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/schedule"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
ESPN_ATHLETE_GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{athlete_id}/gamelog"

HTTP_TIMEOUT = 30
MAX_WORKERS = 6

# Slate window. The NFL week runs Thursday → Monday, so one board covers a
# span of days rather than one date.
SLATE_LOOKAHEAD_DAYS = 8
# Completed games per team used for defense-allowed baselines.
RECENT_GAMES_PER_TEAM = 5
# Rolling history window for the salary board (see module docstring).
HISTORY_SEASONS = 3

NFL_MARKETS = ["PASS_YDS", "RUSH_YDS", "REC_YDS", "REC", "TD", "ML"]

# Only skill positions produce the markets this board quotes.
SKILL_POSITIONS = {"QB", "RB", "FB", "WR", "TE"}


# --------------------------------------------------------------- entry point


def collect_nfl_raw_data(
    data_raw_dir: Path,
    *,
    history_keys: set[str] | None = None,
    history_seasons: int = HISTORY_SEASONS,
) -> dict[str, Any]:
    """Collect one NFL slate. Falls back to the cached payload on any failure.

    ``history_keys`` are normalized player keys (see :func:`player_key`) whose
    multi-season logs the salary board needs. Anyone not in that set is
    collected for the current season only.
    """
    raw_path = data_raw_dir / "nfl_raw.json"
    history_keys = history_keys or set()

    try:
        slate_start, games = fetch_slate_games(today_et())
        if not games:
            raise RuntimeError(f"No NFL games found within {SLATE_LOOKAHEAD_DAYS} days of {slate_start.isoformat()}")

        season_year = season_year_for(games, fallback_date=slate_start)
        season_type_id = detect_season_type_id(games)
        slate_teams = extract_slate_team_map(games)
        roster_map = fetch_team_rosters(slate_teams)
        skill_players = collect_skill_players(roster_map)

        player_profiles = fetch_player_gamelogs(skill_players, seasons=[season_year])
        history_players = [player for player in skill_players if player["player_key"] in history_keys]
        history_profiles = fetch_player_gamelogs(
            history_players,
            seasons=history_season_years(season_year, history_seasons),
        )

        recent_ids = fetch_recent_game_ids(slate_teams, season_year=season_year, season_type_id=season_type_id)
        summary_cache = fetch_game_summaries({game_id for ids in recent_ids.values() for game_id in ids})
        defense_profiles = build_defense_profiles(recent_ids=recent_ids, summary_cache=summary_cache)
        allowance_baselines = build_allowance_baselines(defense_profiles)

        payload = {
            "sport": "NFL",
            "date": slate_start.isoformat(),
            "season_year": season_year,
            "season_type": season_type_label(season_type_id),
            "week": extract_week(games),
            "games": [build_game_payload(event) for event in games],
            "player_profiles": player_profiles,
            "history_profiles": history_profiles,
            "history_seasons": history_season_years(season_year, history_seasons),
            "defense_profiles": defense_profiles,
            "allowance_baselines": allowance_baselines,
        }
        write_json(raw_path, payload)
        return payload
    except Exception:
        if raw_path.exists():
            return json.loads(raw_path.read_text(encoding="utf-8"))
        raise


# ------------------------------------------------------------------- schedule


def fetch_slate_games(start_date) -> tuple[Any, list[dict[str, Any]]]:
    """Every scheduled game in the next ``SLATE_LOOKAHEAD_DAYS`` days.

    One ranged scoreboard call covers the whole football week. If ESPN rejects
    the range form we walk day by day rather than losing the slate.
    """
    end_date = start_date + timedelta(days=SLATE_LOOKAHEAD_DAYS)
    try:
        payload = espn_get_json(
            ESPN_SCOREBOARD_URL,
            {"dates": f"{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}", "limit": 100},
        )
        events = payload.get("events", [])
    except requests.RequestException:
        events = []

    if not events:
        events = []
        for offset in range(0, SLATE_LOOKAHEAD_DAYS):
            day = start_date + timedelta(days=offset)
            try:
                day_payload = espn_get_json(ESPN_SCOREBOARD_URL, {"dates": day.strftime("%Y%m%d")})
            except requests.RequestException:
                continue
            events.extend(day_payload.get("events", []))

    events = dedupe_events(events)
    # Anything already final belongs to last week's board, not this one.
    upcoming = [event for event in events if not is_completed(event)]
    upcoming.sort(key=lambda event: event.get("date", ""))
    return start_date, upcoming


def extract_slate_team_map(games: list[dict[str, Any]]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for event in games:
        for competitor in event["competitions"][0]["competitors"]:
            team = competitor.get("team", {})
            abbr = team.get("abbreviation")
            if abbr:
                mapping[abbr] = int(team["id"])
    return mapping


def build_game_payload(event: dict[str, Any]) -> dict[str, Any]:
    competition = event["competitions"][0]
    away = next(item for item in competition["competitors"] if item["homeAway"] == "away")
    home = next(item for item in competition["competitors"] if item["homeAway"] == "home")
    status_type = competition.get("status", {}).get("type", {})
    return {
        "game_id": str(event["id"]),
        "away_team": away["team"]["abbreviation"],
        "home_team": home["team"]["abbreviation"],
        "away_record": record_win_pct(away.get("records", [])),
        "home_record": record_win_pct(home.get("records", [])),
        "time": status_type.get("shortDetail") or format_kickoff_time(competition["date"]),
        "kickoff": competition.get("date", ""),
        "status": {
            "phase": translate_espn_status(status_type),
            "detailed_state": status_type.get("detail"),
        },
        # Point spread + total when ESPN carries a consensus line. Both feed the
        # simulator's game-script conditioning; absent is fine (None).
        "spread": extract_spread(competition, home_abbr=home["team"]["abbreviation"]),
        "over_under": extract_over_under(competition),
        "indoor": bool(competition.get("venue", {}).get("indoor")),
    }


def extract_spread(competition: dict[str, Any], *, home_abbr: str) -> float | None:
    """Home-team point spread as a signed number (-6.5 means home favored by 6.5)."""
    odds = competition.get("odds") or []
    if not odds:
        return None
    entry = odds[0]
    value = entry.get("spread")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    details = str(entry.get("details") or "")
    match = re.match(r"\s*([A-Z]{2,4})\s+([-+]?\d+(?:\.\d+)?)", details)
    if not match:
        return None
    favored, number = match.group(1), float(match.group(2))
    # `details` is written from the favorite's perspective ("KC -6.5").
    return number if favored == home_abbr else -number


def extract_over_under(competition: dict[str, Any]) -> float | None:
    odds = competition.get("odds") or []
    if not odds:
        return None
    try:
        return float(odds[0].get("overUnder"))
    except (TypeError, ValueError):
        return None


def extract_week(games: list[dict[str, Any]]) -> int | None:
    for event in games:
        week = event.get("week", {}).get("number")
        if week:
            return int(week)
    return None


def season_year_for(games: list[dict[str, Any]], *, fallback_date) -> int:
    for event in games:
        year = event.get("season", {}).get("year")
        if year:
            return int(year)
    return espn_season_year(fallback_date)


def espn_season_year(day) -> int:
    """NFL seasons are labelled by their starting year; Jan/Feb belong to the
    prior label (the 2026 season's Super Bowl is played in Feb 2027)."""
    return day.year if day.month >= 3 else day.year - 1


def history_season_years(season_year: int, seasons: int) -> list[int]:
    """Rolling window, newest first: 2026 → [2026, 2025, 2024]."""
    return [season_year - offset for offset in range(max(seasons, 1))]


def detect_season_type_id(games: list[dict[str, Any]]) -> int:
    types = {int(event.get("season", {}).get("type") or 2) for event in games}
    if 3 in types:
        return 3
    if types == {1}:
        return 1
    return 2


def season_type_label(season_type_id: int) -> str:
    return {1: "Preseason", 2: "Regular Season", 3: "Postseason"}.get(season_type_id, "Regular Season")


# -------------------------------------------------------------------- rosters


def fetch_team_rosters(team_map: dict[str, int]) -> dict[str, list[dict[str, Any]]]:
    def load(item: tuple[str, int]) -> tuple[str, list[dict[str, Any]]]:
        abbr, team_id = item
        try:
            payload = espn_get_json(ESPN_TEAM_ROSTER_URL.format(team_id=team_id))
        except requests.RequestException:
            return abbr, []
        return abbr, list(iter_roster_athletes(payload))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, team_map.items()))


def iter_roster_athletes(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """ESPN returns football rosters grouped by unit (offense/defense/special).

    Basketball returns a flat list. Handle both so the parser survives a shape
    change on either endpoint.
    """
    for entry in payload.get("athletes", []):
        if isinstance(entry, dict) and "items" in entry:
            yield from entry.get("items", [])
        elif isinstance(entry, dict):
            yield entry


def collect_skill_players(roster_map: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Rostered QB/RB/WR/TE tagged with injury status.

    Injured players stay in the pool (tagged) rather than being dropped — the
    board decides what to hide, and an OUT starter is exactly the signal that
    matters for the backup behind them.
    """
    players: list[dict[str, Any]] = []
    for team_abbr, roster in roster_map.items():
        for athlete in roster:
            position = str(athlete.get("position", {}).get("abbreviation") or "").upper()
            if position not in SKILL_POSITIONS:
                continue
            name = athlete.get("displayName") or athlete.get("fullName")
            if not name or not athlete.get("id"):
                continue
            players.append(
                {
                    "athlete_id": str(athlete["id"]),
                    "player_name": name,
                    "player_key": player_key(name),
                    "team": team_abbr,
                    "position": "RB" if position == "FB" else position,
                    "injury_status": extract_injury_status(athlete),
                }
            )
    return players


def player_key(name: str) -> str:
    """Join key between ESPN names and the hand-maintained contracts file.

    Case, punctuation and generational suffixes all vary between sources
    ("Marvin Harrison Jr." vs "Marvin Harrison Jr"), so normalize them away.
    """
    text = str(name or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [token for token in text.split() if token not in {"jr", "sr", "ii", "iii", "iv", "v"}]
    return " ".join(tokens)


# ------------------------------------------------------------------- gamelogs


def fetch_player_gamelogs(players: list[dict[str, Any]], *, seasons: list[int]) -> dict[str, dict[str, Any]]:
    """Player profiles keyed by athlete id, merged across ``seasons``.

    One request per (player, season). Players with no parseable logs in the
    window are dropped rather than carried as empty shells.
    """
    if not players or not seasons:
        return {}

    jobs = [(player, season) for player in players for season in seasons]

    def load(job: tuple[dict[str, Any], int]) -> tuple[str, int, list[dict[str, Any]]]:
        player, season = job
        try:
            payload = espn_get_json(
                ESPN_ATHLETE_GAMELOG_URL.format(athlete_id=player["athlete_id"]),
                {"season": season},
            )
        except requests.RequestException:
            return player["athlete_id"], season, []
        return player["athlete_id"], season, parse_gamelog_events(payload, season=season)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(load, jobs))

    logs_by_athlete: dict[str, list[dict[str, Any]]] = {}
    for athlete_id, _season, logs in results:
        logs_by_athlete.setdefault(athlete_id, []).extend(logs)

    profiles: dict[str, dict[str, Any]] = {}
    for player in players:
        logs = logs_by_athlete.get(player["athlete_id"], [])
        if not logs:
            continue
        profiles[player["athlete_id"]] = build_player_profile(player=player, logs=logs)
    return profiles


def build_player_profile(*, player: dict[str, Any], logs: list[dict[str, Any]]) -> dict[str, Any]:
    logs = dedupe_logs(logs)
    logs.sort(key=lambda log: log["game_date"], reverse=True)
    recent_10 = logs[:10]
    recent_5 = logs[:5]
    return {
        "player_id": player["athlete_id"],
        "player_name": player["player_name"],
        "player_key": player["player_key"],
        "team": player["team"],
        "position": player["position"],
        "injury_status": player.get("injury_status", "ACTIVE"),
        "logs": logs,
        "seasons": sorted({log["season"] for log in logs}, reverse=True),
        "season_avgs": average_log_block(logs),
        "l10_avgs": average_log_block(recent_10),
        "l5_avgs": average_log_block(recent_5),
        "games_played": len(logs),
        "snap_load": average(log["snap_load"] for log in recent_5) if recent_5 else 0.0,
    }


def dedupe_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for log in logs:
        deduped[str(log.get("event_id"))] = log
    return list(deduped.values())


def parse_gamelog_events(payload: dict[str, Any], *, season: int) -> list[dict[str, Any]]:
    names = payload.get("names", [])
    events_meta = payload.get("events", {})
    parsed: list[dict[str, Any]] = []

    for season_type in payload.get("seasonTypes", []):
        for category in season_type.get("categories", []):
            if category.get("type") != "event":
                continue
            for event in category.get("events", []):
                event_id = str(event.get("eventId", ""))
                metadata = events_meta.get(event_id)
                if not metadata:
                    continue
                log = parse_gamelog_event(
                    stats=event.get("stats", []),
                    names=names,
                    metadata=metadata,
                    season=season,
                )
                if log:
                    parsed.append(log)
    return parsed


def parse_gamelog_event(
    *,
    stats: list[str],
    names: list[str],
    metadata: dict[str, Any],
    season: int,
) -> dict[str, Any] | None:
    if not stats or not names or len(stats) != len(names):
        return None

    stat_map = {normalize_stat_name(name): value for name, value in zip(names, stats)}
    game_date = metadata.get("gameDate")
    if not game_date:
        return None

    completions, attempts = split_pair(pick_stat(stat_map, "completions passingattempts", "completionsattempts"))
    pass_yds = pick_number(stat_map, "passingyards", "netpassingyards")
    pass_td = pick_number(stat_map, "passingtouchdowns")
    interceptions = pick_number(stat_map, "interceptions", "passinginterceptions")
    carries = pick_number(stat_map, "rushingattempts", "carries")
    rush_yds = pick_number(stat_map, "rushingyards")
    rush_td = pick_number(stat_map, "rushingtouchdowns")
    receptions = pick_number(stat_map, "receptions")
    targets = pick_number(stat_map, "receivingtargets", "targets")
    rec_yds = pick_number(stat_map, "receivingyards")
    rec_td = pick_number(stat_map, "receivingtouchdowns")
    fumbles_lost = pick_number(stat_map, "fumbleslost", "lostfumbles")

    total_td = pass_td + rush_td + rec_td
    # Touches + targets + dropbacks: the closest thing to a snap count the
    # public gamelog exposes. Used only as a relative opportunity weight.
    snap_load = attempts + carries + targets

    return {
        "event_id": str(metadata.get("id") or metadata.get("eventId") or ""),
        "season": season,
        "game_date": parse_iso_date(game_date),
        "is_home": metadata.get("atVs") != "@",
        "opponent": metadata.get("opponent", {}).get("abbreviation", ""),
        "PASS_ATT": attempts,
        "PASS_CMP": completions,
        "PASS_YDS": pass_yds,
        "PASS_TD": pass_td,
        "INT": interceptions,
        "CARRIES": carries,
        "RUSH_YDS": rush_yds,
        "RUSH_TD": rush_td,
        "REC": receptions,
        "TARGETS": targets,
        "REC_YDS": rec_yds,
        "REC_TD": rec_td,
        "FUM_LOST": fumbles_lost,
        # Scrimmage (non-passing) touchdowns — the "anytime TD" market.
        "TD": rush_td + rec_td,
        "TOTAL_TD": total_td,
        "snap_load": snap_load,
        "fantasy_ppr": ppr_points(
            pass_yds=pass_yds,
            pass_td=pass_td,
            interceptions=interceptions,
            rush_yds=rush_yds,
            rush_td=rush_td,
            receptions=receptions,
            rec_yds=rec_yds,
            rec_td=rec_td,
            fumbles_lost=fumbles_lost,
        ),
    }


def ppr_points(
    *,
    pass_yds: float,
    pass_td: float,
    interceptions: float,
    rush_yds: float,
    rush_td: float,
    receptions: float,
    rec_yds: float,
    rec_td: float,
    fumbles_lost: float,
) -> float:
    """Standard full-PPR scoring.

    The salary board needs one production currency that means the same thing
    for a quarterback and a slot receiver; PPR fantasy points are the
    conventional choice and keep the tier comparisons legible.
    """
    return round(
        (pass_yds * 0.04)
        + (pass_td * 4.0)
        - (interceptions * 2.0)
        + (rush_yds * 0.1)
        + (rush_td * 6.0)
        + receptions
        + (rec_yds * 0.1)
        + (rec_td * 6.0)
        - (fumbles_lost * 2.0),
        2,
    )


STAT_KEYS = (
    "PASS_ATT", "PASS_CMP", "PASS_YDS", "PASS_TD", "INT",
    "CARRIES", "RUSH_YDS", "RUSH_TD",
    "REC", "TARGETS", "REC_YDS", "REC_TD",
    "FUM_LOST", "TD", "TOTAL_TD", "snap_load", "fantasy_ppr",
)


def average_log_block(logs: list[dict[str, Any]]) -> dict[str, float]:
    if not logs:
        return {key: 0.0 for key in STAT_KEYS}
    return {key: round(average(log.get(key, 0.0) for log in logs), 3) for key in STAT_KEYS}


# ------------------------------------------------------------------- defense


def fetch_recent_game_ids(team_map: dict[str, int], *, season_year: int, season_type_id: int) -> dict[str, list[str]]:
    cutoff = now_et()

    def load(item: tuple[str, int]) -> tuple[str, list[str]]:
        abbr, team_id = item
        events: list[dict[str, Any]] = []
        # Preseason boards have no completed regular-season games yet, so also
        # read the prior season rather than shipping empty defense baselines.
        windows = dict.fromkeys(((season_year, season_type_id), (season_year, 2), (season_year - 1, 2)))
        for season, season_type in windows:
            try:
                payload = espn_get_json(
                    ESPN_TEAM_SCHEDULE_URL.format(team_id=team_id),
                    {"season": season, "seasontype": season_type},
                )
            except requests.RequestException:
                continue
            events.extend(payload.get("events", []))
            if len([event for event in dedupe_events(events) if is_completed(event)]) >= RECENT_GAMES_PER_TEAM:
                break

        completed = [
            event
            for event in dedupe_events(events)
            if is_completed(event) and parse_event_datetime(event["date"]) < cutoff
        ]
        completed.sort(key=lambda event: parse_event_datetime(event["date"]), reverse=True)
        return abbr, [str(event["id"]) for event in completed[:RECENT_GAMES_PER_TEAM]]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, team_map.items()))


def fetch_game_summaries(game_ids: set[str]) -> dict[str, dict[str, Any] | None]:
    def load(game_id: str) -> tuple[str, dict[str, Any] | None]:
        try:
            return game_id, espn_get_json(ESPN_SUMMARY_URL, {"event": game_id})
        except requests.RequestException:
            return game_id, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, sorted(game_ids)))


def build_defense_profiles(
    *,
    recent_ids: dict[str, list[str]],
    summary_cache: dict[str, dict[str, Any] | None],
) -> dict[str, dict[str, float]]:
    """What each defense has actually allowed over its last few games."""
    profiles: dict[str, dict[str, float]] = {}

    for team_abbr, game_ids in recent_ids.items():
        allowed_points: list[float] = []
        allowed_pass: list[float] = []
        allowed_rush: list[float] = []
        allowed_total: list[float] = []
        wins: list[float] = []

        for game_id in game_ids:
            payload = summary_cache.get(game_id)
            if not payload:
                continue
            team_boxes = payload.get("boxscore", {}).get("teams", [])
            opponent_box = next(
                (box for box in team_boxes if box.get("team", {}).get("abbreviation") != team_abbr),
                None,
            )
            if opponent_box is None:
                continue
            opponent_stats = boxscore_stat_map(opponent_box.get("statistics", []))
            allowed_pass.append(parse_number(opponent_stats.get("netPassingYards")))
            allowed_rush.append(parse_number(opponent_stats.get("rushingYards")))
            allowed_total.append(parse_number(opponent_stats.get("totalYards")))

            competitors = payload.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
            own = next((item for item in competitors if item.get("team", {}).get("abbreviation") == team_abbr), None)
            opponent = next((item for item in competitors if item.get("team", {}).get("abbreviation") != team_abbr), None)
            if opponent is not None:
                allowed_points.append(parse_number(opponent.get("score")))
            if own is not None:
                wins.append(1.0 if own.get("winner") else 0.0)

        profiles[team_abbr] = {
            "allowed_points": round(average(allowed_points), 2),
            "allowed_pass_yds": round(average(allowed_pass), 2),
            "allowed_rush_yds": round(average(allowed_rush), 2),
            "allowed_total_yds": round(average(allowed_total), 2),
            "recent_win_pct": round(average(wins), 3) if wins else 0.5,
            "sample": len(allowed_total),
        }

    return profiles


def build_allowance_baselines(defense_profiles: dict[str, dict[str, float]]) -> dict[str, float]:
    """League-average allowances — the denominator every matchup ratio uses."""
    scored = [profile for profile in defense_profiles.values() if profile.get("sample")]
    if not scored:
        return {"points": 0.0, "pass_yds": 0.0, "rush_yds": 0.0, "total_yds": 0.0}
    return {
        "points": round(average(profile["allowed_points"] for profile in scored), 2),
        "pass_yds": round(average(profile["allowed_pass_yds"] for profile in scored), 2),
        "rush_yds": round(average(profile["allowed_rush_yds"] for profile in scored), 2),
        "total_yds": round(average(profile["allowed_total_yds"] for profile in scored), 2),
    }


# -------------------------------------------------------------------- helpers


def espn_get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(url, params=params, headers={"User-Agent": "the-board-system/1.0"}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def normalize_stat_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def pick_stat(stat_map: dict[str, Any], *aliases: str) -> Any:
    for alias in aliases:
        key = normalize_stat_name(alias)
        if key in stat_map:
            return stat_map[key]
    return None


def pick_number(stat_map: dict[str, Any], *aliases: str) -> float:
    return parse_number(pick_stat(stat_map, *aliases))


def split_pair(value: Any) -> tuple[float, float]:
    """ESPN packs completions/attempts into one '24/35' cell."""
    if value is None:
        return 0.0, 0.0
    parts = re.split(r"[/-]", str(value))
    if len(parts) < 2:
        return parse_number(value), 0.0
    return parse_number(parts[0]), parse_number(parts[1])


def parse_number(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def average(values) -> float:
    values = [float(value) for value in values]
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def parse_iso_date(value: str) -> str:
    """Store dates as ISO strings — the raw payload round-trips through JSON."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except ValueError:
        return str(value)


def parse_event_datetime(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(now_et().tzinfo)


def format_kickoff_time(value: str) -> str:
    try:
        return parse_event_datetime(value).strftime("%a %I:%M %p ET").replace(" 0", " ")
    except ValueError:
        return "TBD"


def is_completed(event: dict[str, Any]) -> bool:
    competitions = event.get("competitions") or [{}]
    return bool(competitions[0].get("status", {}).get("type", {}).get("completed"))


def translate_espn_status(status_type: dict[str, Any]) -> str:
    state = status_type.get("state")
    if state == "post":
        return "final"
    if state == "in":
        return "live"
    return "pregame"


def dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for event in events:
        deduped[str(event["id"])] = event
    return list(deduped.values())


def boxscore_stat_map(statistics: list[dict[str, Any]]) -> dict[str, Any]:
    return {stat.get("name"): stat.get("displayValue") for stat in statistics}


def record_win_pct(records: list[dict[str, Any]]) -> float:
    for record in records:
        if record.get("type") in {"total", "overall"}:
            try:
                wins, losses = str(record.get("summary", "")).split("-")[:2]
                total = int(wins) + int(losses)
                return int(wins) / total if total else 0.5
            except (ValueError, ZeroDivisionError):
                return 0.5
    return 0.5
