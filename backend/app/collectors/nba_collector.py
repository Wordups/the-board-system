from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import json
from typing import Any

import requests

from app.outputs.json_writer import write_json
from app.utils.dates import now_et, today_et


ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_TEAM_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
ESPN_TEAM_STATS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/statistics"
ESPN_TEAM_SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/schedule"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
ESPN_ATHLETE_GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{athlete_id}/gamelog"
HTTP_TIMEOUT = 30
MAX_WORKERS = 6
NBA_MARKETS = ["PTS", "REB", "AST", "3PM", "ML"]


def collect_nba_raw_data(data_raw_dir: Path) -> dict[str, Any]:
    raw_path = data_raw_dir / "nba_raw.json"
    slate_date = today_et()
    season_year = espn_season_year(slate_date)

    try:
        games = fetch_today_games(slate_date)
        if not games:
            raise RuntimeError(f"No NBA games found for {slate_date.isoformat()}")

        season_type_id = detect_season_type_id(games)
        today_teams = extract_today_team_map(games)
        roster_map = fetch_team_rosters(today_teams)
        active_players = collect_active_players(roster_map)
        player_log_map = fetch_player_gamelogs(active_players, season_year=season_year)
        team_stats_map = fetch_team_statistics(today_teams, season_year=season_year, season_type_id=season_type_id)
        team_summary_profiles = build_team_summary_profiles(
            today_teams=today_teams,
            season_year=season_year,
            season_type_id=season_type_id,
        )
        allowance_baselines = build_allowance_baselines(team_summary_profiles)

        payload = {
            "sport": "NBA",
            "date": slate_date.isoformat(),
            "season_type": "Playoffs" if season_type_id == 3 else "Regular Season",
            "games": [
                build_game_payload(
                    event=event,
                    roster_map=roster_map,
                    player_log_map=player_log_map,
                    team_stats_map=team_stats_map,
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


def fetch_player_gamelogs(players: list[dict[str, Any]], *, season_year: int) -> dict[str, dict[str, Any]]:
    def load(player: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        url = ESPN_ATHLETE_GAMELOG_URL.format(athlete_id=player["athlete_id"])
        payload = espn_get_json(url, {"season": season_year})
        return player["athlete_id"], parse_player_gamelog(player=player, payload=payload)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        loaded = dict(pool.map(load, players))
    return {athlete_id: profile for athlete_id, profile in loaded.items() if profile}


def parse_player_gamelog(*, player: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    labels = payload.get("labels", [])
    names = payload.get("names", [])
    events_meta = payload.get("events", {})
    parsed_logs = []

    for season_type in payload.get("seasonTypes", []):
        for category in season_type.get("categories", []):
            if category.get("type") != "event":
                continue
            for event in category.get("events", []):
                event_id = str(event["eventId"])
                if event_id not in events_meta:
                    continue
                parsed = parse_player_log_event(
                    stats=event.get("stats", []),
                    labels=labels,
                    names=names,
                    metadata=events_meta[event_id],
                )
                if parsed:
                    parsed_logs.append(parsed)

    parsed_logs.sort(key=lambda row: row["game_date"], reverse=True)
    if not parsed_logs:
        return None

    season_avgs = average_log_block(parsed_logs)
    recent_10 = parsed_logs[:10]
    recent_5 = parsed_logs[:5]
    recent_home = [log for log in parsed_logs if log["is_home"]][:10]
    recent_away = [log for log in parsed_logs if not log["is_home"]][:10]

    return {
        "player_id": player["athlete_id"],
        "player_name": player["player_name"],
        "team": player["team"],
        "position": player["position"],
        "logs": parsed_logs,
        "season_avgs": season_avgs,
        "l10_avgs": average_log_block(recent_10),
        "l5_avgs": average_log_block(recent_5),
        "home_avgs": average_log_block(recent_home) if recent_home else season_avgs,
        "away_avgs": average_log_block(recent_away) if recent_away else season_avgs,
        "usage_load": average(log["usage_load"] for log in recent_10) if recent_10 else average(log["usage_load"] for log in parsed_logs),
        "minutes_projection": project_minutes(recent_5, season_avgs["MIN"]),
    }


def parse_player_log_event(*, stats: list[str], labels: list[str], names: list[str], metadata: dict[str, Any]) -> dict[str, Any] | None:
    if not stats or len(stats) != len(labels):
        return None

    stat_map = {name: value for name, value in zip(names, stats)}
    minutes = parse_number(stat_map.get("minutes"))
    if minutes <= 0:
        return None

    fgm, fga = parse_made_attempted(stat_map.get("fieldGoalsMade-fieldGoalsAttempted", "0-0"))
    ftm, fta = parse_made_attempted(stat_map.get("freeThrowsMade-freeThrowsAttempted", "0-0"))
    three_made, three_attempted = parse_made_attempted(stat_map.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0"))
    turnovers = parse_number(stat_map.get("turnovers"))

    return {
        "event_id": str(metadata["id"]),
        "game_date": datetime.fromisoformat(metadata["gameDate"].replace("Z", "+00:00")),
        "is_home": metadata.get("atVs") != "@",
        "opponent": metadata.get("opponent", {}).get("abbreviation", ""),
        "MIN": minutes,
        "PTS": parse_number(stat_map.get("points")),
        "REB": parse_number(stat_map.get("totalRebounds")),
        "AST": parse_number(stat_map.get("assists")),
        "3PM": three_made,
        "FGA": fga,
        "FTA": fta,
        "TOV": turnovers,
        "usage_load": fga + (0.44 * fta) + turnovers,
        "game_result": metadata.get("gameResult", ""),
    }


def fetch_team_statistics(team_map: dict[str, int], *, season_year: int, season_type_id: int) -> dict[str, dict[str, float]]:
    def load(item: tuple[str, int]) -> tuple[str, dict[str, float]]:
        abbr, team_id = item
        payload = espn_get_json(
            ESPN_TEAM_STATS_URL.format(team_id=team_id),
            {"season": season_year, "seasontype": season_type_id},
        )
        return abbr, flatten_team_statistics(payload)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, team_map.items()))


def flatten_team_statistics(payload: dict[str, Any]) -> dict[str, float]:
    flat: dict[str, float] = {}
    stats_root = payload.get("results", {}).get("stats", {})
    for category in stats_root.get("categories", []):
        for stat in category.get("stats", []):
            flat[stat["name"]] = parse_number(stat.get("value"))
    return flat


def build_team_summary_profiles(*, today_teams: dict[str, int], season_year: int, season_type_id: int) -> dict[str, dict[str, float]]:
    recent_ids = fetch_recent_game_ids(today_teams, season_year=season_year, season_type_id=season_type_id)
    summary_cache = fetch_game_summaries({game_id for ids in recent_ids.values() for game_id in ids})
    profiles: dict[str, dict[str, float]] = {}

    for team_abbr, game_ids in recent_ids.items():
        allowed_pts = []
        allowed_reb = []
        allowed_ast = []
        allowed_3pm = []
        recent_wins = []

        for game_id in game_ids:
            payload = summary_cache.get(game_id)
            if not payload:
                continue
            for team_box in payload.get("boxscore", {}).get("teams", []):
                box_abbr = team_box.get("team", {}).get("abbreviation")
                if box_abbr != team_abbr:
                    continue
                opponent_box = next(
                    (
                        item
                        for item in payload.get("boxscore", {}).get("teams", [])
                        if item.get("team", {}).get("abbreviation") != team_abbr
                    ),
                    None,
                )
                if not opponent_box:
                    continue
                opponent_stats = boxscore_stat_map(opponent_box.get("statistics", []))
                allowed_reb.append(parse_number(opponent_stats.get("totalRebounds", 0.0)))
                allowed_ast.append(parse_number(opponent_stats.get("assists", 0.0)))
                allowed_3pm.append(parse_made_attempted(opponent_stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0"))[0])

                result = payload.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
                team_competitor = next((item for item in result if item.get("team", {}).get("abbreviation") == team_abbr), None)
                opponent_competitor = next((item for item in result if item.get("team", {}).get("abbreviation") != team_abbr), None)
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


def fetch_recent_game_ids(team_map: dict[str, int], *, season_year: int, season_type_id: int) -> dict[str, list[str]]:
    def load_schedule(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return espn_get_json(url, params).get("events", [])

    recent_by_team: dict[str, list[str]] = {}
    cutoff = now_et()

    for team_abbr, team_id in team_map.items():
        regular_events = load_schedule(
            ESPN_TEAM_SCHEDULE_URL.format(team_id=team_id),
            {"season": season_year, "seasontype": 2},
        )
        current_events = load_schedule(
            ESPN_TEAM_SCHEDULE_URL.format(team_id=team_id),
            {"season": season_year, "seasontype": season_type_id},
        )

        merged = dedupe_events(current_events + regular_events)
        completed = [
            event
            for event in merged
            if parse_event_datetime(event["date"]) < cutoff
            and event.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("completed")
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
    team_stats_map: dict[str, dict[str, float]],
    team_summary_profiles: dict[str, dict[str, float]],
    allowance_baselines: dict[str, float],
) -> dict[str, Any]:
    competition = event["competitions"][0]
    away_team = next(item for item in competition["competitors"] if item["homeAway"] == "away")
    home_team = next(item for item in competition["competitors"] if item["homeAway"] == "home")
    away_abbr = away_team["team"]["abbreviation"]
    home_abbr = home_team["team"]["abbreviation"]
    series_context = build_series_context(event=event, away_team=away_team, home_team=home_team)

    candidates = []
    candidates.extend(
        build_team_player_candidates(
            game_id=str(event["id"]),
            team_abbr=away_abbr,
            opponent_abbr=home_abbr,
            is_home=False,
            roster=roster_map.get(away_abbr, []),
            player_log_map=player_log_map,
            opponent_team_stats=team_stats_map.get(home_abbr, {}),
            opponent_summary_profile=team_summary_profiles.get(home_abbr, {}),
            allowance_baselines=allowance_baselines,
            series_context=series_context,
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
            opponent_team_stats=team_stats_map.get(away_abbr, {}),
            opponent_summary_profile=team_summary_profiles.get(away_abbr, {}),
            allowance_baselines=allowance_baselines,
            series_context=series_context,
        )
    )
    candidates.extend(
        build_moneyline_candidates(
            game_id=str(event["id"]),
            away_team=away_team,
            home_team=home_team,
            team_summary_profiles=team_summary_profiles,
            team_stats_map=team_stats_map,
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
        "series_context": series_context,
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
    opponent_team_stats: dict[str, float],
    opponent_summary_profile: dict[str, float],
    allowance_baselines: dict[str, float],
    series_context: dict[str, Any],
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
        for market in ["PTS", "REB", "AST", "3PM"]:
            candidate = build_market_candidate(
                market=market,
                game_id=game_id,
                team_abbr=team_abbr,
                opponent_abbr=opponent_abbr,
                is_home=is_home,
                profile=profile,
                usage_share=usage_share,
                opponent_team_stats=opponent_team_stats,
                opponent_summary_profile=opponent_summary_profile,
                allowance_baselines=allowance_baselines,
                series_context=series_context,
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
    opponent_team_stats: dict[str, float],
    opponent_summary_profile: dict[str, float],
    allowance_baselines: dict[str, float],
    series_context: dict[str, Any],
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
    projected = (l5_avg * 0.50) + (l10_avg * 0.30) + (season_avg * 0.15) + (split_avg * 0.05)
    if is_home:
        projected += home_boost_for_market(market)

    line_value = suggested_line(market, projected)
    l10_hit_rate = hit_rate(recent_10, market, line_value)
    l5_hit_rate = hit_rate(recent_5, market, line_value)
    if l10_hit_rate < 0.60:
        return None

    leverage = playoff_leverage_for_team(series_context=series_context, team_abbr=team_abbr)
    minutes_projection = profile["minutes_projection"] + leverage["minutes_boost"]
    minutes_flag = minutes_projection < 30.0
    matchup_ratio = market_matchup_ratio(
        market=market,
        opponent_team_stats=opponent_team_stats,
        opponent_summary_profile=opponent_summary_profile,
        allowance_baselines=allowance_baselines,
    )
    strong_matchup = matchup_ratio >= 1.05
    usage_component = min(usage_share / 0.34, 1.0)
    trend_component = min(max((trend_delta + 4.0) / 8.0, 0.0), 1.0)
    matchup_component = min(max((matchup_ratio - 0.92) / 0.20, 0.0), 1.0)
    home_component = 1.0 if is_home else 0.0

    raw_score = 100 * (
        (l10_hit_rate * 0.27)
        + (l5_hit_rate * 0.27)
        + (usage_component * 0.23)
        + (trend_component * 0.11)
        + (matchup_component * 0.08)
        + (home_component * 0.04)
    )
    raw_score += leverage["score_boost"]
    if market in {"PTS", "AST", "3PM"} and usage_share < 0.30:
        raw_score -= 5.0
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
            is_home=is_home,
            leverage_label=leverage["label"],
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
    team_stats_map: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    away_abbr = away_team["team"]["abbreviation"]
    home_abbr = home_team["team"]["abbreviation"]
    away_profile = team_summary_profiles.get(away_abbr, {})
    home_profile = team_summary_profiles.get(home_abbr, {})
    away_stats = team_stats_map.get(away_abbr, {})
    home_stats = team_stats_map.get(home_abbr, {})

    away_score = ml_score(
        recent_win_pct=away_profile.get("recent_win_pct", 0.5),
        season_record=record_win_pct(away_team.get("records", [])),
        avg_points=away_stats.get("avgPoints", 0.0),
        points_allowed=away_profile.get("allowed_pts", 0.0),
        is_home=False,
    )
    home_score = ml_score(
        recent_win_pct=home_profile.get("recent_win_pct", 0.5),
        season_record=record_win_pct(home_team.get("records", [])),
        avg_points=home_stats.get("avgPoints", 0.0),
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


def market_matchup_ratio(
    *,
    market: str,
    opponent_team_stats: dict[str, float],
    opponent_summary_profile: dict[str, float],
    allowance_baselines: dict[str, float],
) -> float:
    if market == "PTS":
        allowed = opponent_summary_profile.get("allowed_pts", 0.0)
        baseline = allowance_baselines.get("PTS", 0.0)
    elif market == "REB":
        allowed = opponent_summary_profile.get("allowed_reb", 0.0) + max(0.0, 36.0 - opponent_team_stats.get("avgDefensiveRebounds", 36.0))
        baseline = allowance_baselines.get("REB", 0.0)
    elif market == "AST":
        allowed = opponent_summary_profile.get("allowed_ast", 0.0) + max(0.0, 8.0 - opponent_team_stats.get("assistTurnoverRatio", 8.0))
        baseline = allowance_baselines.get("AST", 0.0)
    else:
        allowed = opponent_summary_profile.get("allowed_3pm", 0.0)
        baseline = allowance_baselines.get("3PM", 0.0)
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
) -> str:
    usage_gate = 0.24 if market == "REB" else 0.30
    if l10_hit_rate >= 0.70 and l5_hit_rate >= 0.70 and strong_matchup and minutes_projection >= 30 and usage_share >= usage_gate:
        return "A"
    if l10_hit_rate >= 0.60 and strong_matchup and minutes_projection >= 30:
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
    is_home: bool,
    leverage_label: str,
) -> str:
    parts = [
        f"L10 {l10_hit_rate:.0%}",
        f"L5 {l5_hit_rate:.0%}",
        f"USG {usage_share:.0%}",
        f"MIN {minutes_projection:.1f}",
        f"{market} matchup {matchup_ratio:.2f}x",
    ]
    if is_home:
        parts.append("Home boost")
    if minutes_projection < 30:
        parts.append("Minutes watch")
    if leverage_label:
        parts.append(leverage_label)
    return " | ".join(parts)


def build_series_context(*, event: dict[str, Any], away_team: dict[str, Any], home_team: dict[str, Any]) -> dict[str, Any]:
    competition = event["competitions"][0]
    note = ""
    notes = competition.get("notes") or []
    if notes:
        note = notes[0].get("headline", "")
    series = competition.get("series") or {}
    competitors = series.get("competitors") or []
    wins_by_id = {str(item.get("id")): int(item.get("wins", 0) or 0) for item in competitors}
    away_id = str(away_team["team"]["id"])
    home_id = str(home_team["team"]["id"])
    away_wins = wins_by_id.get(away_id, 0)
    home_wins = wins_by_id.get(home_id, 0)
    game_number = parse_series_game_number(note)
    is_playoffs = event.get("season", {}).get("type") == 3

    return {
        "is_playoffs": is_playoffs,
        "series_summary": series.get("summary", ""),
        "note": note,
        "game_number": game_number,
        "away_team": away_team["team"]["abbreviation"],
        "home_team": home_team["team"]["abbreviation"],
        "away_wins": away_wins,
        "home_wins": home_wins,
    }


def parse_series_game_number(note: str) -> int:
    text = str(note or "")
    if "Game 7" in text:
        return 7
    if "Game 6" in text:
        return 6
    if "Game 5" in text:
        return 5
    if "Game 4" in text:
        return 4
    if "Game 3" in text:
        return 3
    if "Game 2" in text:
        return 2
    if "Game 1" in text:
        return 1
    return 0


def playoff_leverage_for_team(*, series_context: dict[str, Any], team_abbr: str) -> dict[str, Any]:
    if not series_context.get("is_playoffs"):
        return {"minutes_boost": 0.0, "score_boost": 0.0, "label": ""}

    game_number = int(series_context.get("game_number") or 0)
    away_team = series_context.get("away_team")
    home_team = series_context.get("home_team")
    away_wins = int(series_context.get("away_wins") or 0)
    home_wins = int(series_context.get("home_wins") or 0)
    is_away = team_abbr == away_team
    wins = away_wins if is_away else home_wins
    opp_wins = home_wins if is_away else away_wins

    if game_number >= 7:
        return {"minutes_boost": 1.5, "score_boost": 4.0, "label": "Game 7"}
    if opp_wins == 3:
        return {"minutes_boost": 1.1, "score_boost": 2.8, "label": "Elimination game"}
    if wins == 3 and game_number >= 5:
        return {"minutes_boost": 0.6, "score_boost": 1.4, "label": "Closeout spot"}
    if game_number >= 5:
        return {"minutes_boost": 0.35, "score_boost": 0.8, "label": "Late series"}
    return {"minutes_boost": 0.0, "score_boost": 0.0, "label": ""}


def ml_score(*, recent_win_pct: float, season_record: float, avg_points: float, points_allowed: float, is_home: bool) -> float:
    margin_component = min(max(((avg_points - points_allowed) + 12.0) / 24.0, 0.0), 1.0)
    home_component = 0.08 if is_home else 0.0
    return 100 * (
        (recent_win_pct * 0.44)
        + (season_record * 0.26)
        + (margin_component * 0.22)
        + home_component
    )


def detect_season_type_id(games: list[dict[str, Any]]) -> int:
    return 3 if any(event.get("season", {}).get("type") == 3 for event in games) else 2


def translate_espn_status(status_type: dict[str, Any]) -> str:
    state = status_type.get("state")
    if state == "post":
        return "final"
    if state == "in":
        return "live"
    return "pregame"


def espn_season_year(today) -> int:
    return today.year if today.month <= 8 else today.year + 1


def espn_get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(url, params=params, headers={"User-Agent": "the-board-system/1.0"}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def parse_event_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(now_et().tzinfo)


def format_tipoff_time(value: str) -> str:
    return parse_event_datetime(value).strftime("%I:%M %p ET").lstrip("0")


def dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped = {}
    for event in events:
        deduped[str(event["id"])] = event
    return list(deduped.values())


def boxscore_stat_map(statistics: list[dict[str, Any]]) -> dict[str, Any]:
    return {stat["name"]: stat.get("displayValue") for stat in statistics}


def record_win_pct(records: list[dict[str, Any]]) -> float:
    for record in records:
        if record.get("type") == "total":
            summary = record.get("summary", "")
            try:
                wins, losses = summary.split("-")[:2]
                wins_i = int(wins)
                losses_i = int(losses)
                total = wins_i + losses_i
                return wins_i / total if total else 0.5
            except (ValueError, ZeroDivisionError):
                return 0.5
    return 0.5


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
    return (l5_minutes * 0.72) + (season_minutes * 0.28)


def hit_rate(logs: list[dict[str, Any]], market: str, line: int) -> float:
    if not logs:
        return 0.0
    hits = sum(1 for log in logs if log[market] >= line)
    return hits / len(logs)


def suggested_line(market: str, projection: float) -> int:
    adjustments = {"PTS": 1.5, "REB": 0.8, "AST": 0.8, "3PM": 0.4}
    minimums = {"PTS": 10, "REB": 4, "AST": 2, "3PM": 1}
    return max(minimums[market], int(round(projection - adjustments[market])))


def home_boost_for_market(market: str) -> float:
    return {"PTS": 0.8, "REB": 0.35, "AST": 0.4, "3PM": 0.15}.get(market, 0.0)


def format_market_line(market: str, line: int) -> str:
    return f"{line}+ {market}"


def parse_made_attempted(value: str) -> tuple[float, float]:
    try:
        made, attempted = value.split("-")
        return float(made), float(attempted)
    except (AttributeError, ValueError):
        return 0.0, 0.0


def parse_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def simplify_position(position: str) -> str:
    if not position:
        return "F"
    first = position[0].upper()
    return first if first in {"G", "F", "C"} else "F"


def average(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return float(sum(values) / len(values))
