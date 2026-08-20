from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import json
from typing import Any

import requests

from app.models import nfl_model
from app.outputs.json_writer import write_json
from app.scoring.lineups import extract_injury_status, is_playable
from app.scoring.prob_shrinkage import calibrate_prob
from app.scoring.tiers import assign_nfl_tier
from app.scoring.value import format_implied_odds, hit_rate_to_implied_odds
from app.utils.dates import now_et, today_et


HTTP_TIMEOUT = 30
MAX_WORKERS = 8
# Per-player stat lookups (no inline roster stats on NFL's ESPN endpoint,
# unlike soccer/WNBA) run several hundred requests for a full Week 1 slate —
# a higher pool keeps that bounded to a reasonable wall-clock time without
# hammering ESPN any harder per-request than the other collectors do.
PLAYER_STATS_WORKERS = 12

NFL_MARKETS = ["TD", "RecYds", "RushYds", "REC", "PassTD", "PassYds", "Completions", "INT", "ML"]
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}
# Pass-catcher position groups the boxscore-players position split and
# target-share signal are computed for. QB excluded (not a pass-catcher);
# FB/other excluded (too rare in the per-player receiving category to be a
# meaningful group average, and not worth a fourth bucket).
RECEIVING_POSITION_GROUPS = ("WR", "TE", "RB")
# A player needs at least this many 2025 games played to be featured at all —
# mirrors soccer's minimum_appearances gate. Below this, the shrink formula
# would be reporting almost pure position-prior with no real evidence behind
# it, which isn't a pick worth surfacing. This also means true rookies with
# zero 2025 NFL sample are honestly excluded from Phase 1's board, since
# there is no current-season data yet either (see design doc's Week 1
# uncertainty framing).
MINIMUM_GAMES_PLAYED = 4

# Pass-TD ladder: highest rung ever attempted, and the probability floor a
# rung must clear to be worth showing (below this it's noise, not a longshot).
PASS_TD_MAX_RUNG = 4
PASS_TD_MIN_RUNG_PROBABILITY = 0.05
# Interception ladder: same monotonic-P(k+) ladder logic as PassTD, capped
# lower since even a turnover-prone starter rarely clears 2+ picks at a
# probability worth surfacing.
INTERCEPTIONS_MAX_RUNG = 2
INTERCEPTIONS_MIN_RUNG_PROBABILITY = 0.05
# Anytime-TD ladder: same pattern again, for a multi-score game (2+ TD) as
# its own distinct play from the anytime (1+) floor. Rung 1 keeps its
# original 0.12 gate untouched; TD_MIN_RUNG_PROBABILITY only governs whether
# rung 2+ is worth surfacing.
TD_MAX_RUNG = 3
TD_MIN_RUNG_PROBABILITY = 0.05
# Games of 2025 schedule sampled per team for the recency-weighted power
# rating and the boxscore-derived defense-allowed signal. Capped well below
# the full 17-game season to keep the boxscore fetch volume (one request per
# unique game, shared across two teams) bounded for a 32-team slate.
RECENT_GAMES_SAMPLE = 5

# RB trend-watch: two informational, display-only signals derived from 2025
# game-by-game RB history (see ATHLETE_GAMELOG_URL) that the season-aggregate
# stats above can't surface -- a standout mid-season stretch, and how a back
# finished the season relative to his own full-season rate. Neither feeds
# RushYds/REC/TD scoring; see build_rb_trend_watch.
RB_TREND_WORKERS = 12  # one request per RB -- same bulk-fetch profile as PLAYER_STATS_WORKERS
# Recent-form window size. 5 games mirrors MLB's L5 convention (see
# mlb_collector.py's recent_5 usage) as the nearest precedent in this repo
# for a "recent form vs season baseline" split, even though the mechanics
# differ (NFL's 2025 season is complete history -- this is a fixed look at
# the final 5 games of the season, not a live rolling window).
RB_TREND_WINDOW = 5
# A back needs at least 2x the window logged so "final 5 games" is a real
# subset being compared against a season baseline built from meaningfully
# more than just that same window -- below this the "recent" and "season"
# numbers are mostly restating each other.
RB_TREND_MIN_GAMES = 8
# A back needs at least this many total yards/game in his recent window to be
# eligible for the "trending up" ranking. Without this, a rarely-used back
# going from 4 yds/g to 10 yds/g posts a huge trend_pct (+150%) that's really
# just small-sample noise around a role that doesn't matter — this floor
# requires the recent form to represent a real workload before the pure
# percentage-change ranking even applies. Verified against live 2025 gamelog
# data during development: without this gate the top of the list was
# dominated by backup/inactive-tier backs; 25 yds/g is roughly "has a real
# rotational role," well below a starter's ~80-100+ but well above token
# garbage-time touches.
RB_TREND_MIN_USAGE_YDS = 25.0
RB_TREND_TOP_N = 8

# QB game-by-game history: same ATHLETE_GAMELOG_URL host/endpoint as the RB
# trend-watch fetch above, just a QB-specific stat parser (parse_qb_gamelog)
# since a QB's gamelog `names[]` header is a completely different stat set
# (passing) than an RB's (rushing/receiving). Feeds the per-player
# game-history section the NFL drawer shows on the frontend (see
# player_gamelogs in collect_nfl_raw_data) -- additive/display-only, does not
# feed any market score.
QB_GAMELOG_WORKERS = 12  # one request per QB -- same bulk-fetch profile as RB_TREND_WORKERS

# normal_at_least std overrides for the two QB Normal-modeled markets, named
# here (rather than left as inline literals at their call sites below) so
# app.sim.nfl_qb_stack can import the exact values it needs to recompute each
# market's std when it re-simulates the correlated joint distribution — a
# single source of truth instead of a second hand-copied constant that could
# silently drift from what actually scores these markets on the board.
PASS_YDS_STD_RATIO = 0.33
PASS_YDS_MIN_STD = 45.0
COMPLETIONS_STD_RATIO = 0.22
COMPLETIONS_MIN_STD = 3.0

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_TEAM_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/roster"
ESPN_TEAM_SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/schedule"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
# Core API: per-athlete season statistics, cleanly flattened with real
# per-game averages and a `gamesPlayed` count — unlike the site/v2 roster
# endpoint (no inline `statistics` block for NFL athletes) or the athlete
# `overview` endpoint (career/postseason splits, no per-game averages).
CORE_ATHLETE_STATS_URL = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}/types/2/athletes/{athlete_id}/statistics"
# Per-athlete game-by-game log (rushing/receiving/fumbles lines, one row per
# 2025 game) -- a different host (site.web.api.espn.com) than every other
# URL in this module (site.api.espn.com / sports.core.api.espn.com). Same
# host family soccer_collector.py's ATHLETE_OVERVIEW_URL already uses for a
# different sport/purpose. Live-verified 2026-08-19 against real 2025 RB ids
# (Saquon Barkley, Christian McCaffrey, Bijan Robinson all returned 200 with
# real per-game rushing/receiving stat lines under
# seasonTypes[].categories[].events[]) from this same sandbox, at a time when
# site.api.espn.com (the host the rest of this collector depends on) was
# returning 403 for every request -- confirming this is genuinely a
# different, independently-reachable host, not a retest of the same block.
ATHLETE_GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{athlete_id}/gamelog"


def collect_nfl_raw_data(data_raw_dir: Path) -> dict[str, Any]:
    raw_path = data_raw_dir / "nfl_raw.json"

    try:
        season_year, week_number, events = fetch_target_week()
        if not events:
            payload = {
                "sport": "NFL",
                "date": today_et().isoformat(),
                "season": None,
                "week": None,
                "games": [],
                "rb_trend_watch": {"window": RB_TREND_WINDOW, "best_stretch": [], "trending_up": []},
                "player_gamelogs": {},
            }
            write_json(raw_path, payload)
            return payload

        prior_season = season_year - 1
        team_map = extract_team_map(events)  # abbr -> team_id
        rosters = fetch_team_rosters(team_map)  # abbr -> list[athlete dict], skill positions, OUT dropped
        athlete_ids = {str(athlete["id"]) for roster in rosters.values() for athlete in roster}
        player_stats = fetch_player_stats(athlete_ids, season=prior_season)  # athlete_id -> flattened stat dict
        # RB trend watch: separate game-by-game fetch (site.web.api.espn.com,
        # not the season-aggregate CORE_ATHLETE_STATS_URL above) -- additive,
        # display-only signal, does not feed player_stats or any market score.
        rb_gamelogs = fetch_rb_gamelogs(rosters, season=prior_season)
        rb_trend_watch = build_rb_trend_watch(rb_gamelogs, rosters)
        # QB game-by-game history, same host/fetch pattern as rb_gamelogs
        # just above, with its own stat parser. Merged with the already-
        # fetched rb_gamelogs (no duplicate RB fetch) into one player_id ->
        # {team, position, games} lookup the board builder passes straight
        # through to the frontend for the drawer's game-history section.
        qb_gamelogs = fetch_qb_gamelogs(rosters, season=prior_season)
        player_gamelogs: dict[str, Any] = {
            athlete_id: {"team": entry["team"], "position": "QB", "games": entry["games"]}
            for athlete_id, entry in qb_gamelogs.items()
        }
        player_gamelogs.update(
            {
                athlete_id: {"team": entry["team"], "position": "RB", "games": entry["games"]}
                for athlete_id, entry in rb_gamelogs.items()
            }
        )
        team_schedules = fetch_team_schedules(team_map, season=prior_season)  # abbr -> list of completed game rows
        team_power = build_team_power_profiles(team_schedules)  # abbr -> power profile
        athlete_positions = build_athlete_position_map(rosters)  # athlete_id -> position, league-wide
        # One shared summary fetch, reused for both the defense-allowed signal
        # and the target-share signal below -- no duplicate network calls for
        # the same game_id.
        game_summaries = fetch_recent_game_summaries(team_map, team_schedules)
        defense_allowed = fetch_defense_allowed(
            team_map, team_schedules, game_summaries, athlete_positions
        )  # abbr -> {rush_yds, pass_yds, rec_allowed_by_position} allowed/g
        target_shares = fetch_target_shares(
            team_map, team_schedules, game_summaries, athlete_positions
        )  # abbr -> {WR/TE/RB: share of this team's own targets}
        league_baseline = build_league_baseline(defense_allowed)
        league_baseline_by_position = build_league_baseline_by_position(defense_allowed)

        slate_date = min(
            (parse_event_datetime(event["date"]) for event in events), default=now_et()
        ).astimezone(now_et().tzinfo).date()

        payload = {
            "sport": "NFL",
            "date": slate_date.isoformat(),
            "season": season_year,
            "week": week_number,
            "games": [
                build_game_payload(
                    event=event,
                    rosters=rosters,
                    player_stats=player_stats,
                    team_power=team_power,
                    defense_allowed=defense_allowed,
                    league_baseline=league_baseline,
                    league_baseline_by_position=league_baseline_by_position,
                    target_shares=target_shares,
                )
                for event in events
            ],
            "rb_trend_watch": rb_trend_watch,
            "player_gamelogs": player_gamelogs,
        }
        write_json(raw_path, payload)
        return payload
    except Exception:
        if raw_path.exists():
            return json.loads(raw_path.read_text(encoding="utf-8"))
        raise


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def fetch_nfl_week(season_year: int, week_number: int) -> list[dict[str, Any]]:
    payload = espn_get_json(ESPN_SCOREBOARD_URL, {"year": season_year, "seasontype": 2, "week": week_number})
    events = []
    for event in payload.get("events", []):
        status = event.get("competitions", [{}])[0].get("status", {}).get("type", {})
        if status.get("completed"):
            continue
        events.append(event)
    return events


def fetch_target_week(start_season: int = 2026, start_week: int = 1) -> tuple[int, int, list[dict[str, Any]]]:
    """Week 1 2026 is the real target slate. Falls forward a few weeks only
    as a defensive fallback (e.g. ESPN hasn't posted a week's slate yet);
    does not fall back to a different season."""
    for week_number in range(start_week, start_week + 4):
        try:
            events = fetch_nfl_week(start_season, week_number)
        except requests.RequestException:
            continue
        if events:
            return start_season, week_number, events
    return start_season, start_week, []


def extract_team_map(events: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for event in events:
        for competitor in event["competitions"][0]["competitors"]:
            mapping[competitor["team"]["abbreviation"]] = str(competitor["team"]["id"])
    return mapping


# ---------------------------------------------------------------------------
# Rosters (skill positions only, OUT/DOUBTFUL dropped, GTD kept + flagged)
# ---------------------------------------------------------------------------

def fetch_team_rosters(team_map: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    def load(item: tuple[str, str]) -> tuple[str, list[dict[str, Any]]]:
        abbr, team_id = item
        payload = espn_get_json(ESPN_TEAM_ROSTER_URL.format(team_id=team_id))
        players = []
        for group in payload.get("athletes", []):
            for athlete in group.get("items", []):
                position = (athlete.get("position") or {}).get("abbreviation", "")
                if position not in SKILL_POSITIONS:
                    continue
                status = extract_injury_status(athlete)
                if not is_playable(status):
                    continue
                players.append(
                    {
                        "id": str(athlete["id"]),
                        "displayName": athlete.get("displayName", ""),
                        "position": position,
                        "injury_status": status,
                    }
                )
        return abbr, players

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, sorted(team_map.items())))


# ---------------------------------------------------------------------------
# Prior-season (2025) per-player stats — primary signal for a Week 1 board
# with zero current-season sample.
# ---------------------------------------------------------------------------

def fetch_player_stats(athlete_ids: set[str], *, season: int) -> dict[str, dict[str, float]]:
    def load(athlete_id: str) -> tuple[str, dict[str, float] | None]:
        try:
            payload = espn_get_json(CORE_ATHLETE_STATS_URL.format(season=season, athlete_id=athlete_id))
        except requests.RequestException:
            return athlete_id, None
        return athlete_id, flatten_core_stats(payload)

    with ThreadPoolExecutor(max_workers=PLAYER_STATS_WORKERS) as pool:
        results = pool.map(load, sorted(athlete_ids))
    return {athlete_id: stats for athlete_id, stats in results if stats}


def flatten_core_stats(payload: dict[str, Any]) -> dict[str, float]:
    categories = payload.get("splits", {}).get("categories", [])
    flat: dict[str, float] = {}
    for category in categories:
        for stat in category.get("stats", []):
            flat[stat["name"]] = parse_number(stat.get("value"))
    return flat


# ---------------------------------------------------------------------------
# RB trend watch (2025 game-by-game gamelogs -> best-stretch / trending-up)
# ---------------------------------------------------------------------------

def fetch_rb_gamelogs(rosters: dict[str, list[dict[str, Any]]], *, season: int) -> dict[str, dict[str, Any]]:
    """2025 game-by-game rushing+receiving lines for every RB in the roster
    pool (rosters already skill-position-filtered and OUT/DOUBTFUL-dropped
    by fetch_team_rosters -- same eligible pool the RushYds/REC markets draw
    from). Returns athlete_id -> {"team": abbr, "games": [chronological
    per-game dicts]}. RBs whose gamelog fetch fails or who have no parseable
    regular-season games are simply absent, same degrade-quietly convention
    fetch_player_stats already uses for the season-stats endpoint."""
    rb_team_by_id: dict[str, str] = {
        athlete["id"]: abbr
        for abbr, roster in rosters.items()
        for athlete in roster
        if athlete["position"] == "RB"
    }

    def load(athlete_id: str) -> tuple[str, list[dict[str, float]] | None]:
        try:
            payload = espn_get_json(ATHLETE_GAMELOG_URL.format(athlete_id=athlete_id), {"season": season})
        except requests.RequestException:
            return athlete_id, None
        return athlete_id, parse_rb_gamelog(payload)

    with ThreadPoolExecutor(max_workers=RB_TREND_WORKERS) as pool:
        results = pool.map(load, sorted(rb_team_by_id))

    return {
        athlete_id: {"team": rb_team_by_id[athlete_id], "games": games}
        for athlete_id, games in results
        if games
    }


def parse_rb_gamelog(payload: dict[str, Any]) -> list[dict[str, float]] | None:
    """Regular-season per-game rushing+receiving lines, sorted chronologically
    (earliest week first). ESPN's gamelog response groups games under
    `seasonTypes[].categories[].events[]`, with each event's stat values as a
    flat string array positionally matched to the top-level `names` list
    (e.g. names[1] == "rushingYards" -> stats[1] is that game's rushing
    yards); real per-game week numbers and IDs live in the separate
    top-level `events` dict, keyed by eventId. Postseason is a separate
    seasonTypes entry and deliberately excluded here -- a much smaller,
    non-representative sample that not every RB even has. Returns None if
    there's no usable regular-season entry (e.g. a practice-squad RB with
    zero 2025 snaps)."""
    names = payload.get("names") or []
    events_by_id = payload.get("events") or {}
    season_types = payload.get("seasonTypes") or []

    regular = next(
        (season_type for season_type in season_types if "Regular Season" in (season_type.get("displayName") or "")),
        None,
    )
    if not regular or not regular.get("categories"):
        return None

    try:
        rush_idx = names.index("rushingYards")
        rec_idx = names.index("receivingYards")
    except ValueError:
        return None

    games: list[dict[str, float]] = []
    for event_row in regular["categories"][0].get("events", []):
        stats = event_row.get("stats") or []
        if len(stats) <= max(rush_idx, rec_idx):
            continue
        event_meta = events_by_id.get(event_row.get("eventId"), {})
        week = event_meta.get("week")
        if week is None:
            continue
        rush_yds = parse_number(stats[rush_idx])
        rec_yds = parse_number(stats[rec_idx])
        games.append(
            {
                "week": int(week),
                "rush_yds": rush_yds,
                "rec_yds": rec_yds,
                "total_yds": rush_yds + rec_yds,
            }
        )

    games.sort(key=lambda row: row["week"])
    return games or None


def build_rb_trend_watch(
    gamelogs: dict[str, dict[str, Any]], rosters: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Two RB signals derived from 2025 game-by-game history that the
    season-aggregate stats used everywhere else in this collector can't
    surface. Both are additive/display-only -- neither reads from nor writes
    to RushYds/REC/TD scoring above.

    - best_stretch ("who's done amazing"): the single highest rolling
      RB_TREND_WINDOW-game window of total yards from scrimmage anywhere in
      the season. A bellcow's season total already surfaces via the existing
      RushYds/TD markets sorted by score; this instead captures the shape of
      the season -- a standout sustained stretch a flatter high-average back
      might not have.
    - trending_up ("trending up"): the final RB_TREND_WINDOW games of the
      season (how a back finished) vs. his own full-season average -- a
      momentum/breakout read for entering 2026, distinct from a simple
      season-long rate. Only backs who are actually trending up (positive
      trend_pct) are included.
    """
    name_by_id: dict[str, str] = {
        athlete["id"]: athlete["displayName"]
        for roster in rosters.values()
        for athlete in roster
        if athlete["position"] == "RB"
    }

    best_stretch_rows: list[dict[str, Any]] = []
    trending_rows: list[dict[str, Any]] = []

    for athlete_id, entry in gamelogs.items():
        games = entry["games"]
        games_sampled = len(games)
        if games_sampled < RB_TREND_MIN_GAMES:
            continue
        team = entry["team"]
        name = name_by_id.get(athlete_id, "Unknown")
        season_avg = sum(game["total_yds"] for game in games) / games_sampled

        best_sum: float | None = None
        best_start_week: int | None = None
        best_end_week: int | None = None
        for i in range(0, games_sampled - RB_TREND_WINDOW + 1):
            window = games[i : i + RB_TREND_WINDOW]
            window_sum = sum(game["total_yds"] for game in window)
            if best_sum is None or window_sum > best_sum:
                best_sum = window_sum
                best_start_week = window[0]["week"]
                best_end_week = window[-1]["week"]
        if best_sum is not None:
            best_stretch_rows.append(
                {
                    "player_id": athlete_id,
                    "player_name": name,
                    "team": team,
                    "games_sampled": games_sampled,
                    "best_stretch_total_yds": round(best_sum, 1),
                    "best_stretch_avg_yds": round(best_sum / RB_TREND_WINDOW, 1),
                    "best_stretch_weeks": f"Wk {best_start_week}-{best_end_week}",
                    "season_avg_total_yds": round(season_avg, 1),
                }
            )

        recent_games = games[-RB_TREND_WINDOW:]
        recent_avg = sum(game["total_yds"] for game in recent_games) / RB_TREND_WINDOW
        if season_avg > 0 and recent_avg >= RB_TREND_MIN_USAGE_YDS:
            trend_pct = (recent_avg - season_avg) / season_avg
            if trend_pct > 0:
                trending_rows.append(
                    {
                        "player_id": athlete_id,
                        "player_name": name,
                        "team": team,
                        "games_sampled": games_sampled,
                        "season_avg_total_yds": round(season_avg, 1),
                        "recent_avg_total_yds": round(recent_avg, 1),
                        "trend_pct": round(trend_pct, 3),
                        "recent_weeks": f"Wk {recent_games[0]['week']}-{recent_games[-1]['week']}",
                    }
                )

    best_stretch_rows.sort(key=lambda row: row["best_stretch_avg_yds"], reverse=True)
    trending_rows.sort(key=lambda row: row["trend_pct"], reverse=True)

    return {
        "window": RB_TREND_WINDOW,
        "best_stretch": best_stretch_rows[:RB_TREND_TOP_N],
        "trending_up": trending_rows[:RB_TREND_TOP_N],
    }


# ---------------------------------------------------------------------------
# QB game-by-game history (2025 gamelogs -> per-player game log for the
# frontend drawer)
# ---------------------------------------------------------------------------

def fetch_qb_gamelogs(rosters: dict[str, list[dict[str, Any]]], *, season: int) -> dict[str, dict[str, Any]]:
    """2025 game-by-game passing (+ rushing) lines for every QB in the
    roster pool (rosters already skill-position-filtered and OUT/DOUBTFUL-
    dropped by fetch_team_rosters). Same ATHLETE_GAMELOG_URL host/endpoint
    fetch_rb_gamelogs already uses -- just a QB-specific stat parser
    (parse_qb_gamelog) since a QB's gamelog `names[]` header is a completely
    different stat set (passing) than an RB's (rushing/receiving). Returns
    athlete_id -> {"team": abbr, "games": [chronological per-game dicts]}.
    Fetched for every roster QB (not just the eventual starting QB -- that's
    only resolved later, per-game, by find_starting_qb_id), same as
    fetch_rb_gamelogs fetches every roster RB rather than pre-filtering to
    a "starter." A QB whose gamelog fetch fails or who has no parseable
    regular-season games is simply absent, same degrade-quietly convention
    fetch_rb_gamelogs already uses."""
    qb_team_by_id: dict[str, str] = {
        athlete["id"]: abbr
        for abbr, roster in rosters.items()
        for athlete in roster
        if athlete["position"] == "QB"
    }

    def load(athlete_id: str) -> tuple[str, list[dict[str, Any]] | None]:
        try:
            payload = espn_get_json(ATHLETE_GAMELOG_URL.format(athlete_id=athlete_id), {"season": season})
        except requests.RequestException:
            return athlete_id, None
        return athlete_id, parse_qb_gamelog(payload)

    with ThreadPoolExecutor(max_workers=QB_GAMELOG_WORKERS) as pool:
        results = pool.map(load, sorted(qb_team_by_id))

    return {
        athlete_id: {"team": qb_team_by_id[athlete_id], "games": games}
        for athlete_id, games in results
        if games
    }


def parse_qb_gamelog(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Regular-season per-game passing (+ rushing) lines, sorted
    chronologically (earliest week first). Same response shape
    parse_rb_gamelog documents (`names[]` positionally matches each event's
    `stats[]`; real per-game week number, opponent, and result live in the
    separate top-level `events` dict keyed by eventId) -- just a different
    stat subset. Live-verified 2026-08-19 against a real QB (Lamar Jackson,
    athlete id 3916387, 2025 season, this same sandbox): the real `names[]`
    array returned was exactly ["completions", "passingAttempts",
    "passingYards", "completionPct", "yardsPerPassAttempt",
    "passingTouchdowns", "interceptions", "longPassing", "sacks",
    "QBRating", "adjQBR", "rushingAttempts", "rushingYards",
    "yardsPerRushAttempt", "rushingTouchdowns", "longRushing"], confirmed
    against all 13 of his real 2025 regular-season game rows (e.g. Week 1 at
    BUF: 14 completions, 209 pass yds, 2 pass TD, 0 INT, 70 rush yds -- a
    real result the collector had never had access to before this parser).
    Postseason is a separate seasonTypes entry and deliberately excluded
    here, same as parse_rb_gamelog. Returns None if there's no usable
    regular-season entry."""
    names = payload.get("names") or []
    events_by_id = payload.get("events") or {}
    season_types = payload.get("seasonTypes") or []

    regular = next(
        (season_type for season_type in season_types if "Regular Season" in (season_type.get("displayName") or "")),
        None,
    )
    if not regular or not regular.get("categories"):
        return None

    try:
        completions_idx = names.index("completions")
        pass_yds_idx = names.index("passingYards")
        pass_td_idx = names.index("passingTouchdowns")
        int_idx = names.index("interceptions")
        rush_yds_idx = names.index("rushingYards")
    except ValueError:
        return None
    max_idx = max(completions_idx, pass_yds_idx, pass_td_idx, int_idx, rush_yds_idx)

    games: list[dict[str, Any]] = []
    for event_row in regular["categories"][0].get("events", []):
        stats = event_row.get("stats") or []
        if len(stats) <= max_idx:
            continue
        event_meta = events_by_id.get(event_row.get("eventId"), {})
        week = event_meta.get("week")
        if week is None:
            continue
        opponent = (event_meta.get("opponent") or {}).get("abbreviation", "")
        games.append(
            {
                "week": int(week),
                "opponent": opponent,
                "result": event_meta.get("gameResult", ""),
                "completions": parse_number(stats[completions_idx]),
                "pass_yds": parse_number(stats[pass_yds_idx]),
                "pass_td": parse_number(stats[pass_td_idx]),
                "interceptions": parse_number(stats[int_idx]),
                "rush_yds": parse_number(stats[rush_yds_idx]),
            }
        )

    games.sort(key=lambda row: row["week"])
    return games or None


# ---------------------------------------------------------------------------
# Team schedules (2025) -> power rating inputs + defense-allowed sampling
# ---------------------------------------------------------------------------

def fetch_team_schedules(team_map: dict[str, str], *, season: int) -> dict[str, list[dict[str, Any]]]:
    def load(item: tuple[str, str]) -> tuple[str, list[dict[str, Any]]]:
        abbr, team_id = item
        try:
            payload = espn_get_json(ESPN_TEAM_SCHEDULE_URL.format(team_id=team_id), {"season": season})
        except requests.RequestException:
            return abbr, []
        rows = []
        for event in payload.get("events", []):
            competition = event.get("competitions", [{}])[0]
            status = competition.get("status", {}).get("type", {})
            if not status.get("completed"):
                continue
            competitors = competition.get("competitors", [])
            me = next((row for row in competitors if row["team"]["id"] == team_id), None)
            if not me:
                continue
            rows.append(
                {
                    "game_id": str(event["id"]),
                    "team_id": team_id,
                    "date": event.get("date", ""),
                    "won": bool(me.get("winner")),
                    "points_for": parse_number(me.get("score")),
                    "points_against": parse_number(
                        next((row.get("score") for row in competitors if row["team"]["id"] != team_id), 0)
                    ),
                }
            )
        rows.sort(key=lambda row: row["date"], reverse=True)
        return abbr, rows

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, sorted(team_map.items())))


def build_team_power_profiles(team_schedules: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float]]:
    profiles: dict[str, dict[str, float]] = {}
    for abbr, games in team_schedules.items():
        if not games:
            profiles[abbr] = {"power": 0.0, "win_pct": 0.5, "point_diff_per_game": 0.0}
            continue
        recent = games[:RECENT_GAMES_SAMPLE]
        win_pct = average(1.0 if row["won"] else 0.0 for row in games)
        point_diff = average(row["points_for"] - row["points_against"] for row in games)
        recent_point_diff = average(row["points_for"] - row["points_against"] for row in recent)
        power = nfl_model.team_power_rating(
            point_diff_per_game=point_diff,
            recent_point_diff_per_game=recent_point_diff,
            win_pct=win_pct,
        )
        profiles[abbr] = {"power": power, "win_pct": win_pct, "point_diff_per_game": point_diff}
    return profiles


def fetch_recent_game_summaries(
    team_map: dict[str, str], team_schedules: dict[str, list[dict[str, Any]]]
) -> dict[str, dict[str, Any]]:
    """The set of ESPN game summaries needed to sample every team's
    RECENT_GAMES_SAMPLE most recent games -- shared by fetch_defense_allowed
    and fetch_target_shares so the same game_id is never fetched twice."""
    game_ids_needed: set[str] = set()
    for games in team_schedules.values():
        for row in games[:RECENT_GAMES_SAMPLE]:
            game_ids_needed.add(row["game_id"])
    return fetch_summaries(game_ids_needed)


def fetch_defense_allowed(
    team_map: dict[str, str],
    team_schedules: dict[str, list[dict[str, Any]]],
    summaries: dict[str, dict[str, Any]] | None = None,
    athlete_positions: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Boxscore-sampled defense-allowed signal: for each team's most recent
    games, the OPPONENT's own rushing/passing yards in that game are what
    this team's defense allowed. Team-level (run game vs pass game) exactly
    as before, PLUS (new) a position-split breakdown of the same signal:
    how many receiving yards/receptions/TDs the opponent's WRs, TEs, and RBs
    specifically put up against this defense, from the individual boxscore
    lines in `boxscore.players[]` of the same summary responses -- see
    parse_receiving_lines for the honest caveat on how that field was
    verified (or wasn't). Falls back to an empty (sample=0) position split
    per game that lacks a players breakdown, so a defense's rush_yds/pass_yds
    are completely unaffected by whether the deeper per-player data is
    available.
    """
    if summaries is None:
        summaries = fetch_recent_game_summaries(team_map, team_schedules)
    athlete_positions = athlete_positions or {}

    allowed: dict[str, dict[str, Any]] = {}
    for abbr, team_id in team_map.items():
        games = team_schedules.get(abbr, [])[:RECENT_GAMES_SAMPLE]
        rush_allowed = []
        pass_allowed = []
        position_samples: dict[str, list[dict[str, float]]] = {pos: [] for pos in RECEIVING_POSITION_GROUPS}
        for row in games:
            summary = summaries.get(row["game_id"])
            if not summary:
                continue
            teams = summary.get("boxscore", {}).get("teams", [])
            opponent_block = next((block for block in teams if str(block.get("team", {}).get("id")) != team_id), None)
            if not opponent_block:
                continue
            stat_map = {stat["name"]: parse_number(stat.get("value")) for stat in opponent_block.get("statistics", [])}
            if "rushingYards" in stat_map:
                rush_allowed.append(stat_map["rushingYards"])
            if "netPassingYards" in stat_map:
                pass_allowed.append(stat_map["netPassingYards"])

            lines = parse_receiving_lines(summary)
            if lines is not None:
                by_position = summarize_receiving_by_position(
                    lines, team_id_filter=team_id, include_own_team=False, athlete_positions=athlete_positions
                )
                empty_bucket = {"rec": 0.0, "rec_yds": 0.0, "rec_td": 0.0, "targets": 0.0}
                for position in RECEIVING_POSITION_GROUPS:
                    position_samples[position].append(by_position.get(position, empty_bucket))

        rec_allowed_by_position: dict[str, dict[str, float]] = {}
        for position in RECEIVING_POSITION_GROUPS:
            samples = position_samples[position]
            rec_allowed_by_position[position] = {
                "rec_yds": average(item["rec_yds"] for item in samples) if samples else 0.0,
                "rec": average(item["rec"] for item in samples) if samples else 0.0,
                "rec_td": average(item["rec_td"] for item in samples) if samples else 0.0,
                "sample": len(samples),
            }

        allowed[abbr] = {
            "rush_yds": average(rush_allowed) if rush_allowed else 0.0,
            "pass_yds": average(pass_allowed) if pass_allowed else 0.0,
            "sample": len(rush_allowed),
            "rec_allowed_by_position": rec_allowed_by_position,
        }
    return allowed


def fetch_target_shares(
    team_map: dict[str, str],
    team_schedules: dict[str, list[dict[str, Any]]],
    summaries: dict[str, dict[str, Any]] | None = None,
    athlete_positions: dict[str, str] | None = None,
) -> dict[str, dict[str, float]]:
    """How a team's own offense has distributed its targets across
    WR/TE/RB over its own sampled recent games -- e.g. "62% of this team's
    targets go to WRs, 24% to TEs, 14% to RBs". Falls back to receptions
    (see parse_receiving_lines) when a clean target count isn't exposed.
    A raw signal only: exposed on REC/RecYds candidate rows as
    `target_share_pg` by build_team_player_candidates, not wired into any
    probability math here or by this function.
    """
    if summaries is None:
        summaries = fetch_recent_game_summaries(team_map, team_schedules)
    athlete_positions = athlete_positions or {}

    shares: dict[str, dict[str, float]] = {}
    for abbr, team_id in team_map.items():
        games = team_schedules.get(abbr, [])[:RECENT_GAMES_SAMPLE]
        totals = {position: 0.0 for position in RECEIVING_POSITION_GROUPS}
        games_with_data = 0
        for row in games:
            summary = summaries.get(row["game_id"])
            lines = parse_receiving_lines(summary) if summary else None
            if lines is None:
                continue
            games_with_data += 1
            by_position = summarize_receiving_by_position(
                lines, team_id_filter=team_id, include_own_team=True, athlete_positions=athlete_positions
            )
            for position in RECEIVING_POSITION_GROUPS:
                bucket = by_position.get(position)
                if bucket:
                    totals[position] += bucket["targets"]

        grand_total = sum(totals.values())
        if grand_total <= 0:
            shares[abbr] = {"sample": games_with_data}
            continue
        result = {position: round(totals[position] / grand_total, 4) for position in RECEIVING_POSITION_GROUPS}
        result["sample"] = games_with_data
        shares[abbr] = result
    return shares


def parse_receiving_lines(summary: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Per-athlete receiving stat lines from `boxscore.players[]` in an ESPN
    summary response -- one row per pass-catcher per team, for one game.

    VERIFICATION NOTE: this sandbox could not reach site.api.espn.com at all
    (403 on every request, standard UA and a browser UA alike -- looks like
    an IP-level block on this environment, not a UA check) to confirm the
    live shape of this field. This parses ESPN's widely-documented public
    boxscore shape (`statistics[].labels[]` header names next to
    `statistics[].athletes[].stats[]` value arrays, matched by label text
    rather than a hardcoded index) defensively: any missing/unexpected
    structure returns None immediately rather than a guessed partial parse,
    and every caller in this module treats None as "no per-player data for
    this game" and falls back to the pre-existing team-level-only behavior
    for it.
    """
    if not summary:
        return None
    players_blocks = summary.get("boxscore", {}).get("players")
    if not isinstance(players_blocks, list) or not players_blocks:
        return None

    lines: list[dict[str, Any]] = []
    try:
        for team_block in players_blocks:
            team_id = str((team_block.get("team") or {}).get("id", ""))
            for category in team_block.get("statistics", []):
                if category.get("name") != "receiving":
                    continue
                headers = category.get("labels") or category.get("names") or category.get("keys") or []
                header_index = {str(header).strip().upper(): idx for idx, header in enumerate(headers)}
                rec_idx = header_index.get("REC")
                yds_idx = header_index.get("YDS")
                td_idx = header_index.get("TD")
                tgt_idx = header_index.get("TGTS", header_index.get("TARGETS"))
                for entry in category.get("athletes", []):
                    athlete = entry.get("athlete") or {}
                    stats = entry.get("stats") or []
                    position_field = athlete.get("position")
                    position = position_field.get("abbreviation") if isinstance(position_field, dict) else position_field
                    lines.append(
                        {
                            "team_id": team_id,
                            "athlete_id": str(athlete.get("id", "")),
                            "position": position,
                            "rec": stat_value(stats, rec_idx) or 0.0,
                            "rec_yds": stat_value(stats, yds_idx) or 0.0,
                            "rec_td": stat_value(stats, td_idx) or 0.0,
                            "targets": stat_value(stats, tgt_idx),
                        }
                    )
    except (AttributeError, TypeError, KeyError, IndexError):
        return None
    return lines


def stat_value(stats: list[Any], idx: int | None) -> float | None:
    if idx is None or idx >= len(stats):
        return None
    return parse_number(stats[idx])


def summarize_receiving_by_position(
    lines: list[dict[str, Any]],
    *,
    team_id_filter: str,
    include_own_team: bool,
    athlete_positions: dict[str, str],
) -> dict[str, dict[str, float]]:
    """Sum one game's already-parsed receiving lines (see
    parse_receiving_lines) into per-position (WR/TE/RB) totals -- either
    this team's OWN pass-catchers (include_own_team=True, for target share)
    or the OPPOSING team's (include_own_team=False, for defense-allowed).
    Position is resolved from the league-wide roster map first (reliable,
    matches SKILL_POSITIONS) and only falls back to whatever the boxscore
    line itself reports; a line that resolves to neither WR, TE, nor RB is
    dropped rather than guessed into a bucket.
    """
    totals: dict[str, dict[str, float]] = {}
    for line in lines:
        is_own_team = line["team_id"] == team_id_filter
        if is_own_team != include_own_team:
            continue
        position = athlete_positions.get(line["athlete_id"]) or line.get("position")
        if position not in RECEIVING_POSITION_GROUPS:
            continue
        bucket = totals.setdefault(position, {"rec": 0.0, "rec_yds": 0.0, "rec_td": 0.0, "targets": 0.0})
        bucket["rec"] += line["rec"]
        bucket["rec_yds"] += line["rec_yds"]
        bucket["rec_td"] += line["rec_td"]
        bucket["targets"] += line["targets"] if line["targets"] is not None else line["rec"]
    return totals


def fetch_summaries(game_ids: set[str]) -> dict[str, dict[str, Any]]:
    def load(game_id: str) -> tuple[str, dict[str, Any] | None]:
        try:
            return game_id, espn_get_json(ESPN_SUMMARY_URL, {"event": game_id})
        except requests.RequestException:
            return game_id, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, sorted(game_ids)))


def build_league_baseline(defense_allowed: dict[str, dict[str, float]]) -> dict[str, float]:
    rush_values = [profile["rush_yds"] for profile in defense_allowed.values() if profile["sample"] > 0]
    pass_values = [profile["pass_yds"] for profile in defense_allowed.values() if profile["sample"] > 0]
    return {
        "rush_yds": average(rush_values) if rush_values else 110.0,
        "pass_yds": average(pass_values) if pass_values else 220.0,
    }


def build_league_baseline_by_position(defense_allowed: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    """League-average receiving-yards-allowed rate per position (WR/TE/RB),
    averaged only over teams with a non-empty boxscore-derived sample for
    that position -- mirrors build_league_baseline's exclusion of
    zero-sample teams, just one level deeper (per position instead of
    team-total)."""
    baseline: dict[str, dict[str, float]] = {}
    for position in RECEIVING_POSITION_GROUPS:
        samples = [
            profile["rec_allowed_by_position"][position]
            for profile in defense_allowed.values()
            if profile.get("rec_allowed_by_position", {}).get(position, {}).get("sample", 0) > 0
        ]
        baseline[position] = {
            "rec_yds": average(item["rec_yds"] for item in samples) if samples else 0.0,
            "rec": average(item["rec"] for item in samples) if samples else 0.0,
            "rec_td": average(item["rec_td"] for item in samples) if samples else 0.0,
        }
    return baseline


def build_athlete_position_map(rosters: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """athlete_id -> position across every team's roster, league-wide -- the
    preferred position source for boxscore-players parsing (see
    summarize_receiving_by_position) since it's already filtered/verified by
    fetch_team_rosters rather than trusting whatever shape the boxscore's own
    nested athlete.position field happens to be in."""
    mapping: dict[str, str] = {}
    for roster in rosters.values():
        for athlete in roster:
            mapping[str(athlete["id"])] = athlete["position"]
    return mapping


def find_starting_qb_id(roster: list[dict[str, Any]], player_stats: dict[str, dict[str, float]]) -> str | None:
    """The one QB on this roster whose passing markets (PassTD/PassYds/
    Completions/INT) should actually be built. This collector's roster fetch
    has no depth-chart/starter signal at all (ESPN's site/v2 roster endpoint
    doesn't expose one) - without this, every roster QB who clears
    MINIMUM_GAMES_PLAYED (a real, recurring shape: a team with a genuine
    QB competition, e.g. two-plus young arms who each started several 2025
    games) gets full starter treatment, producing multiple "1+ Pass TD"
    lines for the same team/game as if they're independent live markets,
    which cannot be real - a team has exactly one starting QB in a given
    game. Picks the QB with the most 2025 games played as the best
    available proxy for "the current starter" (ties broken by pass
    completions, then attempts) - imperfect for a genuine in-progress
    QB change, but far more honest than treating every roster arm as live.
    Every other roster QB is excluded from QB-specific markets entirely
    (not merely down-weighted) - a backup who isn't playing doesn't belong
    on the board at all, same as this pipeline already excludes zero-
    sample rookies.
    """
    candidates = []
    for athlete in roster:
        if athlete["position"] != "QB":
            continue
        stats = player_stats.get(athlete["id"])
        if not stats:
            continue
        games_played = stats.get("gamesPlayed", 0.0)
        if games_played < MINIMUM_GAMES_PLAYED:
            continue
        candidates.append(
            (
                games_played,
                stats.get("completions", 0.0),
                stats.get("passingAttempts", stats.get("attempts", 0.0)),
                athlete["id"],
            )
        )
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][3]


# ---------------------------------------------------------------------------
# Game / candidate assembly
# ---------------------------------------------------------------------------

def build_game_payload(
    *,
    event: dict[str, Any],
    rosters: dict[str, list[dict[str, Any]]],
    player_stats: dict[str, dict[str, float]],
    team_power: dict[str, dict[str, float]],
    defense_allowed: dict[str, dict[str, float]],
    league_baseline: dict[str, float],
    league_baseline_by_position: dict[str, dict[str, float]] | None = None,
    target_shares: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    target_shares = target_shares or {}
    competition = event["competitions"][0]
    competitors = competition["competitors"]
    away = next(row for row in competitors if row["homeAway"] == "away")
    home = next(row for row in competitors if row["homeAway"] == "home")
    away_abbr = away["team"]["abbreviation"]
    home_abbr = home["team"]["abbreviation"]

    candidates: list[dict[str, Any]] = []
    candidates.extend(
        build_team_player_candidates(
            game_id=str(event["id"]),
            team_abbr=away_abbr,
            opponent_abbr=home_abbr,
            roster=rosters.get(away_abbr, []),
            player_stats=player_stats,
            opponent_allowed=defense_allowed.get(home_abbr, {}),
            league_baseline=league_baseline,
            league_baseline_by_position=league_baseline_by_position,
            target_share_by_position=target_shares.get(away_abbr),
            starting_qb_id=find_starting_qb_id(rosters.get(away_abbr, []), player_stats),
        )
    )
    candidates.extend(
        build_team_player_candidates(
            game_id=str(event["id"]),
            team_abbr=home_abbr,
            opponent_abbr=away_abbr,
            roster=rosters.get(home_abbr, []),
            player_stats=player_stats,
            opponent_allowed=defense_allowed.get(away_abbr, {}),
            league_baseline=league_baseline,
            league_baseline_by_position=league_baseline_by_position,
            target_share_by_position=target_shares.get(home_abbr),
            starting_qb_id=find_starting_qb_id(rosters.get(home_abbr, []), player_stats),
        )
    )
    candidates.append(
        build_moneyline_candidate(
            game_id=str(event["id"]),
            away_abbr=away_abbr,
            home_abbr=home_abbr,
            away_power=team_power.get(away_abbr, {"power": 0.0}),
            home_power=team_power.get(home_abbr, {"power": 0.0}),
        )
    )

    return {
        "game_id": str(event["id"]),
        "away_team": away_abbr,
        "home_team": home_abbr,
        "time": competition["status"]["type"].get("shortDetail") or format_event_time(competition["date"]),
        "status": {"phase": translate_state(competition["status"]["type"].get("state"))},
        "candidates": candidates,
    }


def compute_position_matchups(
    *,
    opponent_allowed: dict[str, Any],
    league_baseline_by_position: dict[str, dict[str, float]],
    fallback: float,
) -> dict[str, float]:
    """Position-specific receiving matchup ratios (WR/TE/RB), each computed
    the same way team-level pass_matchup already is (see
    nfl_model.matchup_ratio) -- this opponent's allowed receiving-yards rate
    TO THAT POSITION vs the league-average rate allowed to that position.
    Falls back to `fallback` (the caller's team-level pass_matchup, i.e. the
    pre-existing behavior) per position whenever that position's
    boxscore-derived sample is empty -- e.g. no per-player breakdown was
    parseable for any of this opponent's sampled games -- so a parsing gap
    degrades to the old signal rather than fabricating a new one from zeros.
    """
    opponent_by_position = opponent_allowed.get("rec_allowed_by_position") or {}
    matchups: dict[str, float] = {}
    for position in RECEIVING_POSITION_GROUPS:
        opponent_stats = opponent_by_position.get(position, {})
        baseline_stats = league_baseline_by_position.get(position, {})
        if opponent_stats.get("sample", 0) <= 0 or baseline_stats.get("rec_yds", 0.0) <= 0:
            matchups[position] = fallback
        else:
            matchups[position] = nfl_model.matchup_ratio(opponent_stats["rec_yds"], baseline_stats["rec_yds"])
    return matchups


def build_team_player_candidates(
    *,
    game_id: str,
    team_abbr: str,
    opponent_abbr: str,
    roster: list[dict[str, Any]],
    player_stats: dict[str, dict[str, float]],
    opponent_allowed: dict[str, Any],
    league_baseline: dict[str, float],
    league_baseline_by_position: dict[str, dict[str, float]] | None = None,
    target_share_by_position: dict[str, float] | None = None,
    starting_qb_id: str | None = None,
) -> list[dict[str, Any]]:
    rush_matchup = nfl_model.matchup_ratio(opponent_allowed.get("rush_yds", 0.0), league_baseline["rush_yds"])
    pass_matchup = nfl_model.matchup_ratio(opponent_allowed.get("pass_yds", 0.0), league_baseline["pass_yds"])
    # Position-aware receiving matchups (WR/TE/RB), each opponent's allowed
    # rec-yards rate TO THAT POSITION vs the league-average rate allowed to
    # that position -- replaces the blanket pass_matchup (opponent's overall
    # passing yards allowed) for REC/RecYds/the receiving side of TD below,
    # so a defense that's stout against WRs but soft against TEs produces two
    # different numbers instead of one. Falls back to pass_matchup per
    # position whenever that position's boxscore-derived sample is empty
    # (see compute_position_matchups) -- the pre-existing behavior, unchanged.
    position_matchups = compute_position_matchups(
        opponent_allowed=opponent_allowed,
        league_baseline_by_position=league_baseline_by_position or {},
        fallback=pass_matchup,
    )
    target_share_by_position = target_share_by_position or {}

    candidates: list[dict[str, Any]] = []
    for athlete in roster:
        # A team has exactly one starting QB in a given game — a backup who
        # cleared MINIMUM_GAMES_PLAYED (a real shape: a genuine QB
        # competition where two-plus arms each started several 2025 games)
        # doesn't belong on the board at all, not even a lesser role. See
        # find_starting_qb_id's docstring for the full reasoning.
        if athlete["position"] == "QB" and athlete["id"] != starting_qb_id:
            continue
        stats = player_stats.get(athlete["id"])
        if not stats:
            continue
        games_played = stats.get("gamesPlayed", 0.0)
        if games_played < MINIMUM_GAMES_PLAYED:
            continue

        position = athlete["position"]
        rec_matchup = position_matchups.get(position, pass_matchup)
        rush_td_pg = stats.get("rushingTouchdowns", 0.0) / games_played
        rec_td_pg = stats.get("receivingTouchdowns", 0.0) / games_played
        pass_td_pg = stats.get("passingTouchdowns", 0.0) / games_played
        pass_yds_pg = stats.get("passingYardsPerGame", stats.get("passingYards", 0.0) / games_played)
        completions_pg = stats.get("completionsPerGame", stats.get("completions", 0.0) / games_played)
        interceptions_pg = stats.get("interceptionsPerGame", stats.get("interceptions", 0.0) / games_played)
        rush_yds_pg = stats.get("rushingYardsPerGame", stats.get("rushingYards", 0.0) / games_played)
        rec_yds_pg = stats.get("receivingYardsPerGame", stats.get("receivingYards", 0.0) / games_played)
        rec_pg = stats.get("receptions", 0.0) / games_played

        # Index into `candidates` before this athlete's rows are appended, so
        # every row this athlete produces below can be tagged afterward with
        # the raw fields the same-game correlation sim (nfl_same_game.py)
        # needs — position for pass-catcher identification, plus the exact
        # already-computed pass_td_lambda / raw rec_td rate, rather than the
        # sim re-deriving them from score strings. Extra keys that
        # to_board_row()'s allow-list and the frontend pipeline both ignore.
        athlete_candidate_start = len(candidates)

        common = {
            "player_id": athlete["id"],
            "player_name": athlete["displayName"],
            "team": team_abbr,
            "opponent": opponent_abbr,
            "game_id": game_id,
        }
        sample_note = f"2025 GP {int(games_played)} | Status {athlete['injury_status']} | Lineup unconfirmed (Week 1 depth chart)"

        td_lambda = nfl_model.project_td_lambda(
            rush_td_per_game=rush_td_pg,
            rec_td_per_game=rec_td_pg,
            sample_games=games_played,
            position=position,
            rush_matchup=rush_matchup,
            rec_matchup=rec_matchup,
        )
        td_probability = nfl_model.poisson_at_least(td_lambda, 1)
        if td_probability >= 0.12:
            # Ladder multi-TD rungs (2+, 3+) above the anytime floor, same
            # monotonic-P(k+) pattern as PassTD/INT — a bellcow's multi-score
            # game is a real, distinct play from "did he score at all," not
            # just a rounding choice. Rung 1 keeps its exact original gate
            # (>= 0.12) and value unchanged, so this is purely additive.
            for td_line in range(1, TD_MAX_RUNG + 1):
                rung_probability = td_probability if td_line == 1 else nfl_model.poisson_at_least(td_lambda, td_line)
                if td_line > 1 and rung_probability < TD_MIN_RUNG_PROBABILITY:
                    break
                candidates.append(
                    make_candidate(
                        **common,
                        market="TD",
                        line="Anytime TD" if td_line == 1 else f"{td_line}+ TD",
                        probability=rung_probability,
                        reason=f"TD λ {td_lambda:.2f} | Rush match {rush_matchup:.2f}x | Rec match {rec_matchup:.2f}x | {sample_note}",
                    )
            )

        if position != "QB":
            rec_rate = nfl_model.project_rec_rate(
                rec_per_game=rec_pg, sample_games=games_played, position=position, rec_matchup=rec_matchup
            )
            if rec_rate >= 1.2:
                rec_line = 2 if rec_rate < 3.2 else (4 if rec_rate < 5.5 else 6)
                rec_probability = nfl_model.poisson_at_least(rec_rate, rec_line)
                if rec_probability >= 0.30:
                    candidates.append(
                        make_candidate(
                            **common,
                            market="REC",
                            line=f"{rec_line}+ Receptions",
                            probability=rec_probability,
                            reason=f"REC/g {rec_rate:.2f} | Rec match {rec_matchup:.2f}x | {sample_note}",
                        )
                    )

            rec_yds_mean = nfl_model.project_rec_yds_mean(
                rec_yds_per_game=rec_yds_pg, sample_games=games_played, position=position, rec_matchup=rec_matchup
            )
            if rec_yds_mean >= 8.0:
                rec_yds_line = nfl_model.yardage_line(rec_yds_mean)
                rec_yds_probability = nfl_model.normal_at_least(rec_yds_mean, rec_yds_line)
                candidates.append(
                    make_candidate(
                        **common,
                        market="RecYds",
                        line=f"{rec_yds_line}+ Rec Yds",
                        probability=rec_yds_probability,
                        reason=f"Proj {rec_yds_mean:.1f} rec yds/g | Rec match {rec_matchup:.2f}x | {sample_note}",
                    )
                )

        rush_yds_mean = nfl_model.project_rush_yds_mean(
            rush_yds_per_game=rush_yds_pg, sample_games=games_played, position=position, rush_matchup=rush_matchup
        )
        if rush_yds_mean >= 8.0:
            rush_yds_line = nfl_model.yardage_line(rush_yds_mean)
            rush_yds_probability = nfl_model.normal_at_least(rush_yds_mean, rush_yds_line)
            candidates.append(
                make_candidate(
                    **common,
                    market="RushYds",
                    line=f"{rush_yds_line}+ Rush Yds",
                    probability=rush_yds_probability,
                    reason=f"Proj {rush_yds_mean:.1f} rush yds/g | Rush match {rush_matchup:.2f}x | {sample_note}",
                )
            )

        if position == "QB":
            pass_td_lambda = nfl_model.project_pass_td_lambda(
                pass_td_per_game=pass_td_pg, sample_games=games_played, pass_matchup=pass_matchup
            )
            if pass_td_lambda >= 0.4:
                # Ladder every rung (1+, 2+, 3+, ...) instead of picking one
                # line off the lambda — a high-volume passer's 3+ TD ceiling
                # is a real, distinct play from his 1+ TD floor, not just a
                # rounding choice between them. P(k+) is monotonically
                # decreasing in k, so stop once a rung drops below a floor
                # that's still a legible longshot rather than noise.
                for pass_td_line in range(1, PASS_TD_MAX_RUNG + 1):
                    pass_td_probability = nfl_model.poisson_at_least(pass_td_lambda, pass_td_line)
                    if pass_td_probability < PASS_TD_MIN_RUNG_PROBABILITY:
                        break
                    candidates.append(
                        make_candidate(
                            **common,
                            market="PassTD",
                            line=f"{pass_td_line}+ Pass TD",
                            probability=pass_td_probability,
                            reason=f"Pass TD λ {pass_td_lambda:.2f} | Rec match {pass_matchup:.2f}x | {sample_note}",
                        )
                    )

            # Passing yards — the single most-bet NFL QB prop in practice
            # (TeamRankings/every real sportsbook QB prop board leads with
            # this), and was missing entirely before now. Normal approx like
            # RushYds/RecYds, but with a tighter std_ratio: a starter's pass
            # volume is far more consistent week to week than a runner's or
            # receiver's usage (which swings more with game script), so the
            # generic yardage std_ratio (tuned for rush/rec) would overstate
            # game-to-game passing-yardage variance.
            pass_yds_mean = nfl_model.project_pass_yds_mean(
                pass_yds_per_game=pass_yds_pg, sample_games=games_played, pass_matchup=pass_matchup
            )
            if pass_yds_mean >= 100.0:
                pass_yds_line = nfl_model.yardage_line(pass_yds_mean, round_to=5)
                pass_yds_probability = nfl_model.normal_at_least(
                    pass_yds_mean, pass_yds_line, std_ratio=PASS_YDS_STD_RATIO, min_std=PASS_YDS_MIN_STD
                )
                candidates.append(
                    make_candidate(
                        **common,
                        market="PassYds",
                        line=f"{pass_yds_line}+ Pass Yds",
                        probability=pass_yds_probability,
                        reason=f"Proj {pass_yds_mean:.1f} pass yds/g | Rec match {pass_matchup:.2f}x | {sample_note}",
                    )
                )

            # Completions — the fourth standard QB prop (yards/TD/INT/
            # completions is the full set every real sportsbook posts).
            # Normal approx like PassYds; matchup reuses the same pass_matchup
            # proxy (softer pass defense -> easier completions too).
            completions_mean = nfl_model.project_completions_mean(
                completions_per_game=completions_pg, sample_games=games_played, pass_matchup=pass_matchup
            )
            if completions_mean >= 10.0:
                completions_line = nfl_model.yardage_line(completions_mean, round_to=1, share=0.85, minimum=5)
                completions_probability = nfl_model.normal_at_least(
                    completions_mean, completions_line, std_ratio=COMPLETIONS_STD_RATIO, min_std=COMPLETIONS_MIN_STD
                )
                candidates.append(
                    make_candidate(
                        **common,
                        market="Completions",
                        line=f"{completions_line}+ Completions",
                        probability=completions_probability,
                        reason=f"Proj {completions_mean:.1f} completions/g | Rec match {pass_matchup:.2f}x | {sample_note}",
                    )
                )

            # Interceptions — ladder like PassTD (1+, 2+): a genuinely
            # Poisson-shaped rare-count event, not a yardage stat. No
            # matchup multiplier (see project_interceptions_lambda) - a
            # turnover-prone QB grades high here on his own tendency alone,
            # which is the real, intentional framing (this market isn't
            # "good QB play", it's "will he throw one").
            int_lambda = nfl_model.project_interceptions_lambda(
                interceptions_per_game=interceptions_pg, sample_games=games_played
            )
            if int_lambda >= 0.3:
                for int_line in range(1, INTERCEPTIONS_MAX_RUNG + 1):
                    int_probability = nfl_model.poisson_at_least(int_lambda, int_line)
                    if int_probability < INTERCEPTIONS_MIN_RUNG_PROBABILITY:
                        break
                    candidates.append(
                        make_candidate(
                            **common,
                            market="INT",
                            line=f"{int_line}+ INT",
                            probability=int_probability,
                            reason=f"INT λ {int_lambda:.2f} (no matchup adj - takeaway rate not collected) | {sample_note}",
                        )
                    )

        for tagged in candidates[athlete_candidate_start:]:
            tagged["position"] = position
            tagged["games_played"] = games_played
            tagged["rec_td_per_game"] = rec_td_pg
            # Raw TD lambda for every position (not just RB below) -- cheap
            # since it's already computed unconditionally above for the TD
            # ladder gate; a plain-language TD projection for a WR/TE's
            # drawer needs this same number, not just the RB stat stack.
            tagged["td_lambda"] = td_lambda
            if position == "QB":
                tagged["pass_td_lambda"] = pass_td_lambda
                # Raw means (not the posted "beatable" line) for the QB stat-
                # stack correlation sim (app.sim.nfl_qb_stack) — it needs the
                # same mean this candidate's own market probability was
                # scored from so its re-simulated marginal matches exactly,
                # not a recomputation from scratch. Tagged unconditionally
                # (even on non-PassYds/Completions rows, e.g. this QB's TD or
                # RushYds candidates) same as pass_td_lambda already is above;
                # the stack sim only reads these off the PassYds/Completions
                # rows specifically, see extract_qb_stat_stack.
                tagged["pass_yds_mean"] = pass_yds_mean
                tagged["completions_mean"] = completions_mean
                # Raw interception lambda -- same "plain projection" purpose
                # as pass_yds_mean/completions_mean above, not read by any
                # sim (the QB stat stack doesn't model INT).
                tagged["int_lambda"] = int_lambda
                tagged["rush_yds_mean"] = rush_yds_mean
            if position == "RB":
                # Raw means/lambda for the RB stat-stack correlation sim
                # (app.sim.nfl_rb_stack) -- same convention as the QB tags
                # just above: the exact mean/lambda this candidate's own
                # market probability was scored from, not a recomputation.
                # Tagged unconditionally across all of this RB's rows (TD,
                # RushYds, REC, ...), same as pass_td_lambda is above; the
                # stack sim only reads these off the RushYds/RecYds/TD rows
                # specifically, see extract_rb_stat_stack.
                tagged["rush_yds_mean"] = rush_yds_mean
                tagged["rec_yds_mean"] = rec_yds_mean
                tagged["td_lambda"] = td_lambda
            # Raw signal only -- see fetch_target_shares. Not wired into any
            # scoring/probability math; a future consumer (e.g. the same-game
            # sim's receiver-attribution weighting) can read it off the row.
            if tagged["market"] in ("REC", "RecYds") and position in target_share_by_position:
                tagged["target_share_pg"] = target_share_by_position[position]

    return candidates


def build_moneyline_candidate(
    *,
    game_id: str,
    away_abbr: str,
    home_abbr: str,
    away_power: dict[str, float],
    home_power: dict[str, float],
) -> dict[str, Any]:
    home_win_probability = nfl_model.moneyline_win_probability(
        power_a=home_power.get("power", 0.0), power_b=away_power.get("power", 0.0), home_edge=1.0
    )
    if home_win_probability >= 0.5:
        pick_abbr, opponent_abbr, probability = home_abbr, away_abbr, home_win_probability
    else:
        pick_abbr, opponent_abbr, probability = away_abbr, home_abbr, 1.0 - home_win_probability

    calibrated = calibrate_prob(probability, sport="NFL", market="ML")
    score = round((calibrated["model_prob"] or probability) * 100.0, 2)
    implied = hit_rate_to_implied_odds(calibrated["model_prob"] or probability)
    return {
        "player_id": f"{pick_abbr.lower()}-{game_id}-moneyline",
        "player_name": pick_abbr,
        "team": pick_abbr,
        "opponent": opponent_abbr,
        "game_id": game_id,
        "market": "ML",
        "line": "Moneyline",
        "score": score,
        "confidence": clamp_int(score),
        "tier": assign_nfl_tier(score, "ML"),
        "reason": (
            f"Model win prob {calibrated['model_prob_raw']:.1%} -> calibrated {calibrated['model_prob']:.1%} "
            f"| Implied {format_implied_odds(implied)} | 2025 point-diff power rating"
        ),
        "sim_prob_pct": score,
        "model_hit_rate": round(calibrated["model_prob"] or probability, 4),
        "implied_odds": format_implied_odds(implied),
        "lineup_confirmed": False,
    }


def make_candidate(
    *,
    player_id: str,
    player_name: str,
    team: str,
    opponent: str,
    game_id: str,
    market: str,
    line: str,
    probability: float,
    reason: str,
) -> dict[str, Any]:
    probability = nfl_model.clamp_probability(probability)
    calibrated = calibrate_prob(probability, sport="NFL", market=market)
    calibrated_prob = calibrated["model_prob"] if calibrated["model_prob"] is not None else probability
    score = round(calibrated_prob * 100.0, 2)
    return {
        "player_id": str(player_id),
        "player_name": player_name,
        "team": team,
        "opponent": opponent,
        "game_id": str(game_id),
        "market": market,
        "line": line,
        "score": score,
        "confidence": clamp_int(score),
        "tier": assign_nfl_tier(score, market),
        "reason": reason,
        "sim_prob_pct": score,
        "model_hit_rate_raw": round(probability, 4),
        "lineup_confirmed": False,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def translate_state(state: str | None) -> str:
    if state == "post":
        return "final"
    if state == "in":
        return "live"
    return "pregame"


def format_event_time(value: str) -> str:
    event_time = parse_event_datetime(value).astimezone(now_et().tzinfo)
    return event_time.strftime("%a %I:%M %p ET").replace(" 0", " ").lstrip("0")


def parse_event_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def espn_get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(url, params=params, headers={"User-Agent": "the-board-system/1.0"}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def parse_number(value: Any) -> float:
    if isinstance(value, dict):
        return parse_number(value.get("value") if "value" in value else value.get("displayValue"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def average(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def clamp_int(value: float) -> int:
    return max(1, min(99, round(value)))
