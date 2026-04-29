from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import json
from typing import Any

import requests

from app.outputs.json_writer import write_json
from app.utils.dates import today_et
from app.collectors.nba_collector import (
    HTTP_TIMEOUT,
    MAX_WORKERS,
    average,
    dedupe_events,
    format_tipoff_time,
    parse_event_datetime,
    parse_made_attempted,
    parse_number,
    record_win_pct,
    simplify_position,
    translate_espn_status,
)


ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
ESPN_TEAM_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/roster"
ESPN_TEAM_SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/schedule"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"
WNBA_MARKETS = ["PTS", "REB", "AST", "3PM", "ML"]


def collect_wnba_raw_data(data_raw_dir: Path) -> dict[str, Any]:
    raw_path = data_raw_dir / "wnba_raw.json"
    slate_date = today_et()

    try:
        games = fetch_today_games(slate_date)
        if not games:
            raise RuntimeError(f"No WNBA games found for {slate_date.isoformat()}")

        season_type_id = detect_season_type_id(games)
        today_teams = extract_today_team_map(games)
        roster_map = fetch_team_rosters(today_teams)
        active_players = collect_active_players(roster_map)
        recent_game_ids = fetch_recent_game_ids(today_teams, season_year=slate_date.year, season_type_id=season_type_id)
        summary_cache = fetch_game_summaries({game_id for ids in recent_game_ids.values() for game_id in ids})
        player_log_map = build_player_log_map(active_players, recent_game_ids=recent_game_ids, summary_cache=summary_cache)
        team_summary_profiles = build_team_summary_profiles(recent_game_ids=recent_game_ids, summary_cache=summary_cache)
        allowance_baselines = build_allowance_baselines(team_summary_profiles)

        payload = {
            "sport": "WNBA",
            "date": slate_date.isoformat(),
            "season_type": season_type_name(season_type_id),
            "games": [
                build_game_payload(
                    event=event,
                    roster_map=roster_map,
                    player_log_map=player_log_map,
                    team_summary_profiles=team_summary_profiles,
                    allowance_baselines=allowance_baselines,
                )
                for event in games
            ],
        }
        write_json(raw_path, payload)
        return payload
    except Exception:
        if raw_path.exists():
            return json.loads(raw_path.read_text(encoding="utf-8"))
        raise


def fetch_today_games(slate_date) -> list[dict[str, Any]]:
    payload = espn_get_json(ESPN_SCOREBOARD_URL, {"dates": slate_date.strftime("%Y%m%d")})
    return payload.get("events", [])


def extract_today_team_map(games: list[dict[str, Any]]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for event in games:
        for competitor in event["competitions"][0]["competitors"]:
            mapping[competitor["team"]["abbreviation"]] = int(competitor["team"]["id"])
    return mapping


def fetch_team_rosters(team_map: dict[str, int]) -> dict[str, list[dict[str, Any]]]:
    def load(item: tuple[str, int]) -> tuple[str, list[dict[str, Any]]]:
        abbr, team_id = item
        payload = espn_get_json(ESPN_TEAM_ROSTER_URL.format(team_id=team_id))
        return abbr, payload.get("athletes", [])

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, team_map.items()))


def collect_active_players(roster_map: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    players = []
    for team_abbr, roster in roster_map.items():
        for athlete in roster:
            if athlete.get("status", {}).get("type") != "active":
                continue
            if athlete.get("injuries"):
                continue
            players.append(
                {
                    "athlete_id": str(athlete["id"]),
                    "player_name": athlete["displayName"],
                    "team": team_abbr,
                    "position": simplify_position(athlete.get("position", {}).get("abbreviation", "")),
                }
            )
    return players


def fetch_recent_game_ids(team_map: dict[str, int], *, season_year: int, season_type_id: int) -> dict[str, list[str]]:
    recent_by_team: dict[str, list[str]] = {}

    for team_abbr, team_id in team_map.items():
        current_events = espn_get_json(
            ESPN_TEAM_SCHEDULE_URL.format(team_id=team_id),
            {"season": season_year, "seasontype": season_type_id},
        ).get("events", [])
        regular_events = []
        if season_type_id != 2:
            regular_events = espn_get_json(
                ESPN_TEAM_SCHEDULE_URL.format(team_id=team_id),
                {"season": season_year, "seasontype": 2},
            ).get("events", [])

        merged = dedupe_events(current_events + regular_events)
        completed = [
            event
            for event in merged
            if event.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("completed")
        ]
        completed.sort(key=lambda row: parse_event_datetime(row["date"]), reverse=True)
        recent_by_team[team_abbr] = [str(event["id"]) for event in completed[:8]]

    return recent_by_team


def fetch_game_summaries(game_ids: set[str]) -> dict[str, dict[str, Any]]:
    def load(game_id: str) -> tuple[str, dict[str, Any] | None]:
        try:
            return game_id, espn_get_json(ESPN_SUMMARY_URL, {"event": game_id})
        except requests.RequestException:
            return game_id, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, sorted(game_ids)))


def build_player_log_map(
    active_players: list[dict[str, Any]],
    *,
    recent_game_ids: dict[str, list[str]],
    summary_cache: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    active_by_id = {player["athlete_id"]: player for player in active_players}
    logs_by_player: dict[str, list[dict[str, Any]]] = {player["athlete_id"]: [] for player in active_players}

    for team_abbr, game_ids in recent_game_ids.items():
        for game_id in game_ids:
            payload = summary_cache.get(game_id)
            if not payload or "boxscore" not in payload:
                continue
            header_competitors = payload.get("gameInfo", {}).get("competitors") or payload.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
            competitors_by_abbr = {item.get("team", {}).get("abbreviation"): item for item in header_competitors}
            opponent_abbr = next((abbr for abbr in competitors_by_abbr if abbr != team_abbr), "")
            game_date = extract_summary_date(payload)
            team_blocks = payload["boxscore"].get("players", [])
            team_block = next((block for block in team_blocks if block.get("team", {}).get("abbreviation") == team_abbr), None)
            if not team_block:
                continue
            stat_table = next((table for table in team_block.get("statistics", []) if table.get("athletes")), None)
            if not stat_table:
                continue

            for athlete_row in stat_table.get("athletes", []):
                athlete = athlete_row.get("athlete", {})
                athlete_id = str(athlete.get("id", ""))
                if athlete_id not in active_by_id:
                    continue
                parsed = parse_summary_player_log(
                    athlete_row=athlete_row,
                    game_date=game_date,
                    opponent_abbr=opponent_abbr,
                    is_home=competitors_by_abbr.get(team_abbr, {}).get("homeAway") == "home",
                )
                if parsed:
                    logs_by_player[athlete_id].append(parsed)

    player_log_map: dict[str, dict[str, Any]] = {}
    for athlete_id, logs in logs_by_player.items():
        logs.sort(key=lambda row: row["game_date"], reverse=True)
        if not logs:
            continue
        player = active_by_id[athlete_id]
        season_avgs = average_log_block(logs)
        recent_10 = logs[:10]
        recent_5 = logs[:5]
        recent_home = [log for log in logs if log["is_home"]][:10]
        recent_away = [log for log in logs if not log["is_home"]][:10]
        player_log_map[athlete_id] = {
            "player_id": athlete_id,
            "player_name": player["player_name"],
            "team": player["team"],
            "position": player["position"],
            "logs": logs,
            "season_avgs": season_avgs,
            "l10_avgs": average_log_block(recent_10),
            "l5_avgs": average_log_block(recent_5),
            "home_avgs": average_log_block(recent_home) if recent_home else season_avgs,
            "away_avgs": average_log_block(recent_away) if recent_away else season_avgs,
            "usage_load": average(log["usage_load"] for log in recent_10) if recent_10 else average(log["usage_load"] for log in logs),
            "minutes_projection": project_minutes(recent_5, season_avgs["MIN"]),
            "sample_size": len(logs),
        }
    return player_log_map


def parse_summary_player_log(*, athlete_row: dict[str, Any], game_date: datetime, opponent_abbr: str, is_home: bool) -> dict[str, Any] | None:
    stats = athlete_row.get("stats", [])
    if not stats:
        return None
    minutes = parse_number(stats[0])
    if minutes <= 0 or athlete_row.get("didNotPlay"):
        return None
    fgm, fga = parse_made_attempted(stats[2] if len(stats) > 2 else "0-0")
    three_made, three_attempted = parse_made_attempted(stats[3] if len(stats) > 3 else "0-0")
    ftm, fta = parse_made_attempted(stats[4] if len(stats) > 4 else "0-0")
    turnovers = parse_number(stats[7] if len(stats) > 7 else 0)
    return {
        "game_date": game_date,
        "is_home": is_home,
        "opponent": opponent_abbr,
        "MIN": minutes,
        "PTS": parse_number(stats[1] if len(stats) > 1 else 0),
        "REB": parse_number(stats[5] if len(stats) > 5 else 0),
        "AST": parse_number(stats[6] if len(stats) > 6 else 0),
        "3PM": three_made,
        "FGA": fga,
        "FTA": fta,
        "TOV": turnovers,
        "usage_load": fga + (0.44 * fta) + turnovers,
    }


def build_team_summary_profiles(*, recent_game_ids: dict[str, list[str]], summary_cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    profiles: dict[str, dict[str, float]] = {}

    for team_abbr, game_ids in recent_game_ids.items():
        allowed_pts = []
        allowed_reb = []
        allowed_ast = []
        allowed_3pm = []
        recent_wins = []

        for game_id in game_ids:
            payload = summary_cache.get(game_id)
            if not payload or "boxscore" not in payload:
                continue
            box_teams = payload["boxscore"].get("teams", [])
            team_box = next((item for item in box_teams if item.get("team", {}).get("abbreviation") == team_abbr), None)
            opponent_box = next((item for item in box_teams if item.get("team", {}).get("abbreviation") != team_abbr), None)
            if not team_box or not opponent_box:
                continue
            opponent_stats = boxscore_stat_map(opponent_box.get("statistics", []))
            allowed_reb.append(parse_number(opponent_stats.get("totalRebounds", 0.0)))
            allowed_ast.append(parse_number(opponent_stats.get("assists", 0.0)))
            allowed_3pm.append(parse_made_attempted(opponent_stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0"))[0])

            competitors = payload.get("gameInfo", {}).get("competitors") or payload.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
            team_competitor = next((item for item in competitors if item.get("team", {}).get("abbreviation") == team_abbr), None)
            opponent_competitor = next((item for item in competitors if item.get("team", {}).get("abbreviation") != team_abbr), None)
            if opponent_competitor is not None:
                allowed_pts.append(parse_number(opponent_competitor.get("score")))
            if team_competitor is not None:
                recent_wins.append(1.0 if team_competitor.get("winner") else 0.0)

        profiles[team_abbr] = {
            "allowed_pts": average(allowed_pts),
            "allowed_reb": average(allowed_reb),
            "allowed_ast": average(allowed_ast),
            "allowed_3pm": average(allowed_3pm),
            "recent_win_pct": average(recent_wins) if recent_wins else 0.5,
        }

    return profiles


def build_allowance_baselines(team_summary_profiles: dict[str, dict[str, float]]) -> dict[str, float]:
    return {
        "PTS": average(profile["allowed_pts"] for profile in team_summary_profiles.values()),
        "REB": average(profile["allowed_reb"] for profile in team_summary_profiles.values()),
        "AST": average(profile["allowed_ast"] for profile in team_summary_profiles.values()),
        "3PM": average(profile["allowed_3pm"] for profile in team_summary_profiles.values()),
    }


def build_game_payload(
    *,
    event: dict[str, Any],
    roster_map: dict[str, list[dict[str, Any]]],
    player_log_map: dict[str, dict[str, Any]],
    team_summary_profiles: dict[str, dict[str, float]],
    allowance_baselines: dict[str, float],
) -> dict[str, Any]:
    competition = event["competitions"][0]
    away_team = next(item for item in competition["competitors"] if item["homeAway"] == "away")
    home_team = next(item for item in competition["competitors"] if item["homeAway"] == "home")
    away_abbr = away_team["team"]["abbreviation"]
    home_abbr = home_team["team"]["abbreviation"]

    candidates = []
    candidates.extend(
        build_team_player_candidates(
            game_id=str(event["id"]),
            team_abbr=away_abbr,
            opponent_abbr=home_abbr,
            is_home=False,
            roster=roster_map.get(away_abbr, []),
            player_log_map=player_log_map,
            opponent_summary_profile=team_summary_profiles.get(home_abbr, {}),
            allowance_baselines=allowance_baselines,
        )
    )
    candidates.extend(
        build_team_player_candidates(
            game_id=str(event["id"]),
            team_abbr=home_abbr,
            opponent_abbr=away_abbr,
            is_home=True,
            roster=roster_map.get(home_abbr, []),
            player_log_map=player_log_map,
            opponent_summary_profile=team_summary_profiles.get(away_abbr, {}),
            allowance_baselines=allowance_baselines,
        )
    )
    candidates.extend(
        build_moneyline_candidates(
            game_id=str(event["id"]),
            away_team=away_team,
            home_team=home_team,
            team_summary_profiles=team_summary_profiles,
        )
    )

    return {
        "game_id": str(event["id"]),
        "away_team": away_abbr,
        "home_team": home_abbr,
        "time": competition["status"]["type"].get("shortDetail") or competition["status"]["type"].get("detail") or format_tipoff_time(competition["date"]),
        "status": {
            "phase": translate_espn_status(competition["status"]["type"]),
            "detailed_state": competition["status"]["type"].get("detail"),
            "game_clock": competition["status"].get("displayClock"),
            "period": competition["status"].get("period"),
        },
        "candidates": candidates,
    }


def build_team_player_candidates(
    *,
    game_id: str,
    team_abbr: str,
    opponent_abbr: str,
    is_home: bool,
    roster: list[dict[str, Any]],
    player_log_map: dict[str, dict[str, Any]],
    opponent_summary_profile: dict[str, float],
    allowance_baselines: dict[str, float],
) -> list[dict[str, Any]]:
    active_profiles = []
    for athlete in roster:
        if athlete.get("status", {}).get("type") != "active":
            continue
        if athlete.get("injuries"):
            continue
        profile = player_log_map.get(str(athlete["id"]))
        if not profile:
            continue
        active_profiles.append(profile)

    team_usage_total = sum(profile["usage_load"] for profile in active_profiles) or 1.0
    candidates = []

    for profile in active_profiles:
        usage_share = min((profile["usage_load"] / team_usage_total) * 1.8, 0.45)
        for market in ("PTS", "REB", "AST", "3PM"):
            candidate = build_market_candidate(
                market=market,
                game_id=game_id,
                team_abbr=team_abbr,
                opponent_abbr=opponent_abbr,
                is_home=is_home,
                profile=profile,
                usage_share=usage_share,
                opponent_summary_profile=opponent_summary_profile,
                allowance_baselines=allowance_baselines,
            )
            if candidate:
                candidates.append(candidate)

    return candidates


def build_market_candidate(
    *,
    market: str,
    game_id: str,
    team_abbr: str,
    opponent_abbr: str,
    is_home: bool,
    profile: dict[str, Any],
    usage_share: float,
    opponent_summary_profile: dict[str, float],
    allowance_baselines: dict[str, float],
) -> dict[str, Any] | None:
    logs = profile["logs"]
    recent_10 = logs[:10]
    recent_5 = logs[:5]
    if not recent_10:
        return None

    season_avg = profile["season_avgs"][market]
    l10_avg = average(log[market] for log in recent_10)
    l5_avg = average(log[market] for log in recent_5)
    split_avg = profile["home_avgs"][market] if is_home else profile["away_avgs"][market]
    trend_delta = l5_avg - season_avg
    projected = (l5_avg * 0.48) + (l10_avg * 0.27) + (season_avg * 0.15) + (split_avg * 0.10)
    if is_home:
        projected += home_boost_for_market(market)

    line_value = suggested_line(market, projected)
    l10_hit_rate = hit_rate(recent_10, market, line_value)
    l5_hit_rate = hit_rate(recent_5, market, line_value)
    sample_size = max(1, profile.get("sample_size", len(recent_10)))
    sample_factor = min(sample_size / 5.0, 1.0)
    if sample_size >= 3 and l10_hit_rate < 0.55:
        return None

    minutes_projection = profile["minutes_projection"]
    minutes_flag = minutes_projection < 24.0
    matchup_ratio = market_matchup_ratio(
        market=market,
        opponent_summary_profile=opponent_summary_profile,
        allowance_baselines=allowance_baselines,
    )
    strong_matchup = matchup_ratio >= 1.05
    usage_component = min(usage_share / (0.28 if market == "PTS" else 0.22), 1.0)
    trend_component = min(max((trend_delta + 3.0) / 6.0, 0.0), 1.0)
    matchup_component = min(max((matchup_ratio - 0.92) / 0.20, 0.0), 1.0)
    home_component = 1.0 if is_home else 0.0

    raw_score = 100 * (
        (l10_hit_rate * 0.27)
        + (l5_hit_rate * 0.22)
        + (usage_component * 0.20)
        + (trend_component * 0.11)
        + (matchup_component * 0.10)
        + (home_component * 0.04)
        + (sample_factor * 0.06)
    )
    if market in {"PTS", "AST", "3PM"} and usage_share < 0.24:
        raw_score -= 4.0
    if minutes_flag:
        raw_score -= 4.0

    raw_score = max(raw_score, 1.0)
    tier = classify_prop_tier(
        market=market,
        l10_hit_rate=l10_hit_rate,
        l5_hit_rate=l5_hit_rate,
        usage_share=usage_share,
        strong_matchup=strong_matchup,
        minutes_projection=minutes_projection,
        sample_size=sample_size,
    )

    return {
        "player_id": str(profile["player_id"]),
        "player_name": profile["player_name"],
        "team": team_abbr,
        "opponent": opponent_abbr,
        "game_id": game_id,
        "market": market,
        "line": format_market_line(market, line_value),
        "score": round(raw_score, 2),
        "confidence": max(1, min(99, round(raw_score))),
        "tier": tier,
        "reason": build_market_reason(
            market=market,
            l10_hit_rate=l10_hit_rate,
            l5_hit_rate=l5_hit_rate,
            usage_share=usage_share,
            minutes_projection=minutes_projection,
            matchup_ratio=matchup_ratio,
            sample_size=sample_size,
            is_home=is_home,
        ),
        "l10_hit_rate": l10_hit_rate,
        "l5_hit_rate": l5_hit_rate,
        "usage_pct": usage_share,
        "minutes_projection": minutes_projection,
        "strong_matchup": strong_matchup,
    }


def build_moneyline_candidates(
    *,
    game_id: str,
    away_team: dict[str, Any],
    home_team: dict[str, Any],
    team_summary_profiles: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    away_abbr = away_team["team"]["abbreviation"]
    home_abbr = home_team["team"]["abbreviation"]
    away_profile = team_summary_profiles.get(away_abbr, {})
    home_profile = team_summary_profiles.get(home_abbr, {})

    away_score = ml_score(
        recent_win_pct=away_profile.get("recent_win_pct", 0.5),
        season_record=record_win_pct(away_team.get("records", [])),
        points_allowed=away_profile.get("allowed_pts", 0.0),
        is_home=False,
    )
    home_score = ml_score(
        recent_win_pct=home_profile.get("recent_win_pct", 0.5),
        season_record=record_win_pct(home_team.get("records", [])),
        points_allowed=home_profile.get("allowed_pts", 0.0),
        is_home=True,
    )

    if home_score >= away_score:
        pick_abbr, opp_abbr, score, profile = home_abbr, away_abbr, home_score, home_profile
    else:
        pick_abbr, opp_abbr, score, profile = away_abbr, home_abbr, away_score, away_profile

    tier = "A" if score >= 72 else "B" if score >= 64 else "C"
    return [
        {
            "player_id": f"{pick_abbr.lower()}-moneyline",
            "player_name": pick_abbr,
            "team": pick_abbr,
            "opponent": opp_abbr,
            "game_id": game_id,
            "market": "ML",
            "line": "Moneyline",
            "score": round(score, 2),
            "confidence": max(1, min(99, round(score))),
            "tier": tier,
            "reason": f"Recent win {profile.get('recent_win_pct', 0.5):.0%} | Allow {profile.get('allowed_pts', 0.0):.1f} PPG",
            "l10_hit_rate": profile.get("recent_win_pct", 0.5),
            "l5_hit_rate": profile.get("recent_win_pct", 0.5),
        }
    ]


def market_matchup_ratio(*, market: str, opponent_summary_profile: dict[str, float], allowance_baselines: dict[str, float]) -> float:
    key_map = {
        "PTS": "allowed_pts",
        "REB": "allowed_reb",
        "AST": "allowed_ast",
        "3PM": "allowed_3pm",
    }
    allowed = opponent_summary_profile.get(key_map[market], 0.0)
    baseline = allowance_baselines.get(market, 0.0)
    if baseline <= 0:
        return 1.0
    return max(0.75, min(1.25, allowed / baseline))


def classify_prop_tier(
    *,
    market: str,
    l10_hit_rate: float,
    l5_hit_rate: float,
    usage_share: float,
    strong_matchup: bool,
    minutes_projection: float,
    sample_size: int,
) -> str:
    usage_gate = 0.18 if market == "REB" else 0.24
    if sample_size >= 3 and l10_hit_rate >= 0.70 and l5_hit_rate >= 0.60 and strong_matchup and minutes_projection >= 24 and usage_share >= usage_gate:
        return "A"
    if l10_hit_rate >= 0.60 and minutes_projection >= 22:
        return "B"
    return "C"


def build_market_reason(
    *,
    market: str,
    l10_hit_rate: float,
    l5_hit_rate: float,
    usage_share: float,
    minutes_projection: float,
    matchup_ratio: float,
    sample_size: int,
    is_home: bool,
) -> str:
    parts = [
        f"Sample {sample_size}",
        f"L10 {l10_hit_rate:.0%}",
        f"L5 {l5_hit_rate:.0%}",
        f"USG {usage_share:.0%}",
        f"MIN {minutes_projection:.1f}",
        f"{market} matchup {matchup_ratio:.2f}x",
    ]
    if is_home:
        parts.append("Home boost")
    if minutes_projection < 24:
        parts.append("Minutes watch")
    return " | ".join(parts)


def ml_score(*, recent_win_pct: float, season_record: float, points_allowed: float, is_home: bool) -> float:
    defense_component = min(max((90.0 - points_allowed) / 18.0, 0.0), 1.0)
    home_component = 0.08 if is_home else 0.0
    return 100 * (
        (recent_win_pct * 0.46)
        + (season_record * 0.28)
        + (defense_component * 0.18)
        + home_component
    )


def season_type_name(season_type_id: int) -> str:
    return {
        1: "Preseason",
        2: "Regular Season",
        3: "Playoffs",
    }.get(season_type_id, "Regular Season")


def detect_season_type_id(games: list[dict[str, Any]]) -> int:
    event_type = next((event.get("season", {}).get("type") for event in games if event.get("season", {}).get("type")), 2)
    return int(event_type)


def extract_summary_date(payload: dict[str, Any]) -> datetime:
    game_date = payload.get("gameInfo", {}).get("date") or payload.get("header", {}).get("competitions", [{}])[0].get("date")
    return datetime.fromisoformat(game_date.replace("Z", "+00:00"))


def average_log_block(logs: list[dict[str, Any]]) -> dict[str, float]:
    if not logs:
        return {"PTS": 0.0, "REB": 0.0, "AST": 0.0, "3PM": 0.0, "MIN": 0.0}
    return {
        "PTS": average(log["PTS"] for log in logs),
        "REB": average(log["REB"] for log in logs),
        "AST": average(log["AST"] for log in logs),
        "3PM": average(log["3PM"] for log in logs),
        "MIN": average(log["MIN"] for log in logs),
    }


def project_minutes(recent_logs: list[dict[str, Any]], season_minutes: float) -> float:
    if not recent_logs:
        return season_minutes
    l5_minutes = average(log["MIN"] for log in recent_logs)
    return (l5_minutes * 0.70) + (season_minutes * 0.30)


def hit_rate(logs: list[dict[str, Any]], market: str, line: int) -> float:
    if not logs:
        return 0.0
    hits = sum(1 for log in logs if log[market] >= line)
    return hits / len(logs)


def suggested_line(market: str, projection: float) -> int:
    adjustments = {"PTS": 1.0, "REB": 0.6, "AST": 0.6, "3PM": 0.4}
    minimums = {"PTS": 8, "REB": 3, "AST": 2, "3PM": 1}
    return max(minimums[market], int(round(projection - adjustments[market])))


def home_boost_for_market(market: str) -> float:
    return {"PTS": 0.5, "REB": 0.25, "AST": 0.25, "3PM": 0.1}.get(market, 0.0)


def format_market_line(market: str, line: int) -> str:
    return f"{line}+ {market}"


def boxscore_stat_map(statistics: list[dict[str, Any]]) -> dict[str, Any]:
    return {stat["name"]: stat.get("displayValue") for stat in statistics}


def espn_get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(url, params=params, headers={"User-Agent": "the-board-system/1.0"}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()
