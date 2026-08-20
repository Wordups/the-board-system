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

NFL_MARKETS = ["TD", "RecYds", "RushYds", "REC", "PassTD", "ML"]
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}
# A player needs at least this many 2025 games played to be featured at all —
# mirrors soccer's minimum_appearances gate. Below this, the shrink formula
# would be reporting almost pure position-prior with no real evidence behind
# it, which isn't a pick worth surfacing. This also means true rookies with
# zero 2025 NFL sample are honestly excluded from Phase 1's board, since
# there is no current-season data yet either (see design doc's Week 1
# uncertainty framing).
MINIMUM_GAMES_PLAYED = 4
# Games of 2025 schedule sampled per team for the recency-weighted power
# rating and the boxscore-derived defense-allowed signal. Capped well below
# the full 17-game season to keep the boxscore fetch volume (one request per
# unique game, shared across two teams) bounded for a 32-team slate.
RECENT_GAMES_SAMPLE = 5

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_TEAM_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/roster"
ESPN_TEAM_SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/schedule"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"
# Core API: per-athlete season statistics, cleanly flattened with real
# per-game averages and a `gamesPlayed` count — unlike the site/v2 roster
# endpoint (no inline `statistics` block for NFL athletes) or the athlete
# `overview` endpoint (career/postseason splits, no per-game averages).
CORE_ATHLETE_STATS_URL = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}/types/2/athletes/{athlete_id}/statistics"


def collect_nfl_raw_data(data_raw_dir: Path) -> dict[str, Any]:
    raw_path = data_raw_dir / "nfl_raw.json"

    try:
        season_year, week_number, events = fetch_target_week()
        if not events:
            payload = {"sport": "NFL", "date": today_et().isoformat(), "season": None, "week": None, "games": []}
            write_json(raw_path, payload)
            return payload

        prior_season = season_year - 1
        team_map = extract_team_map(events)  # abbr -> team_id
        rosters = fetch_team_rosters(team_map)  # abbr -> list[athlete dict], skill positions, OUT dropped
        athlete_ids = {str(athlete["id"]) for roster in rosters.values() for athlete in roster}
        player_stats = fetch_player_stats(athlete_ids, season=prior_season)  # athlete_id -> flattened stat dict
        team_schedules = fetch_team_schedules(team_map, season=prior_season)  # abbr -> list of completed game rows
        team_power = build_team_power_profiles(team_schedules)  # abbr -> power profile
        defense_allowed = fetch_defense_allowed(team_map, team_schedules)  # abbr -> {rush_yds, pass_yds} allowed/g
        league_baseline = build_league_baseline(defense_allowed)

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
                )
                for event in events
            ],
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


def fetch_defense_allowed(team_map: dict[str, str], team_schedules: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float]]:
    """Boxscore-sampled defense-allowed signal: for each team's most recent
    games, the OPPONENT's own rushing/passing yards in that game are what
    this team's defense allowed. Team-level (run game vs pass game), not
    split further by RB/WR/TE — ESPN's public site API doesn't expose a
    clean per-position 'allowed' stat, so this is the honest resolution
    available without scraping every player's boxscore line individually.
    """
    game_ids_needed: set[str] = set()
    for games in team_schedules.values():
        for row in games[:RECENT_GAMES_SAMPLE]:
            game_ids_needed.add(row["game_id"])

    summaries = fetch_summaries(game_ids_needed)

    allowed: dict[str, dict[str, float]] = {}
    for abbr, team_id in team_map.items():
        games = team_schedules.get(abbr, [])[:RECENT_GAMES_SAMPLE]
        rush_allowed = []
        pass_allowed = []
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
        allowed[abbr] = {
            "rush_yds": average(rush_allowed) if rush_allowed else 0.0,
            "pass_yds": average(pass_allowed) if pass_allowed else 0.0,
            "sample": len(rush_allowed),
        }
    return allowed


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
) -> dict[str, Any]:
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


def build_team_player_candidates(
    *,
    game_id: str,
    team_abbr: str,
    opponent_abbr: str,
    roster: list[dict[str, Any]],
    player_stats: dict[str, dict[str, float]],
    opponent_allowed: dict[str, float],
    league_baseline: dict[str, float],
) -> list[dict[str, Any]]:
    rush_matchup = nfl_model.matchup_ratio(opponent_allowed.get("rush_yds", 0.0), league_baseline["rush_yds"])
    pass_matchup = nfl_model.matchup_ratio(opponent_allowed.get("pass_yds", 0.0), league_baseline["pass_yds"])

    candidates: list[dict[str, Any]] = []
    for athlete in roster:
        stats = player_stats.get(athlete["id"])
        if not stats:
            continue
        games_played = stats.get("gamesPlayed", 0.0)
        if games_played < MINIMUM_GAMES_PLAYED:
            continue

        position = athlete["position"]
        rush_td_pg = stats.get("rushingTouchdowns", 0.0) / games_played
        rec_td_pg = stats.get("receivingTouchdowns", 0.0) / games_played
        pass_td_pg = stats.get("passingTouchdowns", 0.0) / games_played
        rush_yds_pg = stats.get("rushingYardsPerGame", stats.get("rushingYards", 0.0) / games_played)
        rec_yds_pg = stats.get("receivingYardsPerGame", stats.get("receivingYards", 0.0) / games_played)
        rec_pg = stats.get("receptions", 0.0) / games_played

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
            rec_matchup=pass_matchup,
        )
        td_probability = nfl_model.poisson_at_least(td_lambda, 1)
        if td_probability >= 0.12:
            candidates.append(
                make_candidate(
                    **common,
                    market="TD",
                    line="Anytime TD",
                    probability=td_probability,
                    reason=f"TD λ {td_lambda:.2f} | Rush match {rush_matchup:.2f}x | Rec match {pass_matchup:.2f}x | {sample_note}",
                )
            )

        if position != "QB":
            rec_rate = nfl_model.project_rec_rate(
                rec_per_game=rec_pg, sample_games=games_played, position=position, rec_matchup=pass_matchup
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
                            reason=f"REC/g {rec_rate:.2f} | Rec match {pass_matchup:.2f}x | {sample_note}",
                        )
                    )

            rec_yds_mean = nfl_model.project_rec_yds_mean(
                rec_yds_per_game=rec_yds_pg, sample_games=games_played, position=position, rec_matchup=pass_matchup
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
                        reason=f"Proj {rec_yds_mean:.1f} rec yds/g | Rec match {pass_matchup:.2f}x | {sample_note}",
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
                pass_td_line = 2 if pass_td_lambda >= 1.55 else 1
                pass_td_probability = nfl_model.poisson_at_least(pass_td_lambda, pass_td_line)
                candidates.append(
                    make_candidate(
                        **common,
                        market="PassTD",
                        line=f"{pass_td_line}+ Pass TD",
                        probability=pass_td_probability,
                        reason=f"Pass TD λ {pass_td_lambda:.2f} | Rec match {pass_matchup:.2f}x | {sample_note}",
                    )
                )

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
