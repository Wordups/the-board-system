from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import json
from typing import Any

import requests

from app.outputs.json_writer import write_json
from app.scoring.lineups import (
    compute_team_lineup_context,
    extract_injury_status,
    is_playable,
    lineup_summary_note,
)
from app.scoring.value import (
    find_value_line,
    format_implied_odds,
    value_zone,
)
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
    requested_date = today_et()
    slate_date = requested_date
    season_year = espn_season_year(slate_date)

    try:
        slate_date, games = fetch_target_games(requested_date)
        if not games:
            raise RuntimeError(f"No NBA games found for {slate_date.isoformat()}")
        season_year = espn_season_year(slate_date)

        season_type_id = detect_season_type_id(games)
        today_teams = extract_today_team_map(games)
        roster_map = fetch_team_rosters(today_teams)
        active_players = collect_active_players(roster_map)
        recent_ids = fetch_recent_game_ids(today_teams, season_year=season_year, season_type_id=season_type_id)
        summary_cache = fetch_game_summaries({game_id for ids in recent_ids.values() for game_id in ids})
        player_log_map = fetch_player_gamelogs(active_players, season_year=season_year)
        enrich_logs_with_quarter_data(player_log_map, summary_cache)
        for _profile in player_log_map.values():
            _profile["quarter_profile"] = quarter_avgs_from_logs(_profile["logs"])
        team_stats_map = fetch_team_statistics(today_teams, season_year=season_year, season_type_id=season_type_id)
        team_summary_profiles = build_team_summary_profiles(
            today_teams=today_teams,
            recent_ids=recent_ids,
            summary_cache=summary_cache,
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


def fetch_target_games(start_date) -> tuple[Any, list[dict[str, Any]]]:
    for offset in range(0, 8):
        slate_date = start_date + timedelta(days=offset)
        games = fetch_today_games(slate_date)
        if games:
            return slate_date, games
    return start_date, []


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
    """Return all rostered players tagged with their current injury status.

    Previously this dropped any athlete with a non-empty `injuries` field,
    which silently removed players like Joel Embiid when Out — and removed
    the signal needed to boost teammates' usage. Now we keep them in the
    pool with `injury_status` so downstream code can decide what to drop
    and how to redistribute usage.
    """
    players = []
    for team_abbr, roster in roster_map.items():
        for athlete in roster:
            if athlete.get("status", {}).get("type") != "active":
                continue  # waived / not on roster
            players.append(
                {
                    "athlete_id": str(athlete["id"]),
                    "player_name": athlete["displayName"],
                    "team": team_abbr,
                    "position": simplify_position(athlete.get("position", {}).get("abbreviation", "")),
                    "injury_status": extract_injury_status(athlete),
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
        "injury_status": player.get("injury_status", "ACTIVE"),
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


def build_team_summary_profiles(*, today_teams: dict[str, int], recent_ids: dict[str, list[str]], summary_cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
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
    # Pull every rostered player's profile + injury status, even ones who
    # are out tonight, so we can compute the team's lineup picture.
    roster_profiles = []
    for athlete in roster:
        if athlete.get("status", {}).get("type") != "active":
            continue
        profile = player_log_map.get(str(athlete["id"]))
        if not profile:
            continue
        # Roster's injury_status is fresher than the gamelog's snapshot
        profile["injury_status"] = extract_injury_status(athlete)
        roster_profiles.append(profile)

    lineup_context = compute_team_lineup_context(roster_profiles)
    boost_factor = float(lineup_context.get("boost_factor", 1.0))
    star_outs = lineup_context.get("star_outs", [])

    active_profiles = [p for p in roster_profiles if is_playable(p.get("injury_status", "ACTIVE"))]
    team_usage_total = sum(profile["usage_load"] for profile in active_profiles) or 1.0
    candidates = []

    for profile in active_profiles:
        base_share = min((profile["usage_load"] / team_usage_total) * 1.8, 0.45)
        usage_share = min(base_share * boost_factor, 0.55)
        status = profile.get("injury_status", "ACTIVE")
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
            if not candidate:
                continue
            # Annotate with lineup-awareness signals so the frontend and the
            # picks snapshot can surface them.
            candidate["lineup_status"] = status
            candidate["team_star_outs"] = list(star_outs)
            # Don't surface a player as their own GTD teammate
            candidate["team_star_gtd"] = [
                name for name in lineup_context.get("star_gtd", [])
                if name != profile.get("player_name")
            ]
            candidate["team_usage_boost"] = round(boost_factor, 3)
            candidate["team_lost_usage"] = round(float(lineup_context.get("lost_usage", 0.0)), 2)
            note = lineup_summary_note(lineup_context, status)
            if note:
                candidate["reason"] = f"{candidate['reason']} | {note}"
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
    vs_opp_logs = [log for log in logs if log["opponent"] == opponent_abbr][:8]
    vs_opp_avg = average(log[market] for log in vs_opp_logs) if vs_opp_logs else season_avg
    trend_delta = l5_avg - season_avg
    projected = (l5_avg * 0.46) + (l10_avg * 0.26) + (season_avg * 0.14) + (split_avg * 0.04) + (vs_opp_avg * 0.10)
    if is_home:
        projected += home_boost_for_market(market)

    # Anchor the line search at the player's career baseline (max of season /
    # vs-opponent / l10), not engineered floor — same pattern as WNBA.
    baseline = max(season_avg, vs_opp_avg, l10_avg, 0.0)
    if baseline <= 0:
        baseline = max(season_avg, 1.0)
    valued = find_value_line(
        market=market,
        recent_logs=recent_10,
        baseline=baseline,
        projection=projected,
        line_minimums=NBA_LINE_MINIMUMS,
    )
    if not valued:
        return None
    line_value = valued["line"]
    model_hit_rate = valued["hit_rate"]
    implied_odds_value = valued["implied_odds"]
    edge = valued["edge"]
    zone = valued["zone"]
    vs_opp_hit_rate = hit_rate(vs_opp_logs, market, line_value) if vs_opp_logs else model_hit_rate

    q_profile = profile.get("quarter_profile", {})
    q4_avg = float(q_profile.get("q4_avg", 0.0))
    q4_share = float(q_profile.get("q4_share", 0.0))
    q4_closer = market == "PTS" and q4_share >= 0.28 and int(q_profile.get("sample", 0)) >= 4

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
    h2h_confidence = min(len(vs_opp_logs) / 4.0, 1.0)
    h2h_component = min(max((vs_opp_hit_rate - 0.50) / 0.40, 0.0), 1.0) * h2h_confidence
    home_component = 1.0 if is_home else 0.0

    # Value-aware score: peak around AIM (~0.50 hit rate), bonuses for
    # matchup / usage / trend / h2h / home, value-zone bonus on top.
    aim_proximity = max(0.0, 1.0 - abs(model_hit_rate - 0.50) * 2.0)
    raw_score = 100 * (
        (aim_proximity * 0.30)
        + (usage_component * 0.20)
        + (matchup_component * 0.15)
        + (trend_component * 0.10)
        + (h2h_component * 0.05)
        + (home_component * 0.05)
    )
    raw_score += leverage["score_boost"]
    raw_score += NBA_VALUE_ZONE_BONUS.get(zone, 0.0)
    if q4_closer:
        raw_score += 3.0
    if market in {"PTS", "AST", "3PM"} and usage_share < 0.30:
        raw_score -= 5.0
    if minutes_flag:
        raw_score -= 4.0
    raw_score = max(raw_score, 1.0)

    tier = classify_prop_tier(
        market=market,
        l10_hit_rate=model_hit_rate,
        l5_hit_rate=model_hit_rate,
        usage_share=usage_share,
        strong_matchup=strong_matchup,
        minutes_projection=minutes_projection,
        zone=zone,
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
        "implied_odds": format_implied_odds(implied_odds_value),
        "implied_odds_value": implied_odds_value,
        "value_zone": zone,
        "edge": edge,
        "model_hit_rate": round(model_hit_rate, 3),
        "reason": build_market_reason(
            market=market,
            model_hit_rate=model_hit_rate,
            implied_odds=implied_odds_value,
            edge=edge,
            zone=zone,
            usage_share=usage_share,
            minutes_projection=minutes_projection,
            matchup_ratio=matchup_ratio,
            is_home=is_home,
            leverage_label=leverage["label"],
            vs_opp_hit_rate=vs_opp_hit_rate,
            vs_opp_games=len(vs_opp_logs),
            vs_opp_avg=vs_opp_avg,
            opponent_abbr=opponent_abbr,
            projected=projected,
            baseline=baseline,
            q4_avg=q4_avg,
            q4_closer=q4_closer,
        ),
        "quarter_profile": q_profile,
        # Legacy fields for shared NBA board builder consumers.
        "l10_hit_rate": model_hit_rate,
        "l5_hit_rate": model_hit_rate,
        "vs_opp_hit_rate": vs_opp_hit_rate,
        "usage_pct": usage_share,
        "minutes_projection": minutes_projection,
        "strong_matchup": strong_matchup,
    }


# Per-market line floors (no point asking for over 0.5 PTS, etc.).
NBA_LINE_MINIMUMS = {"PTS": 10, "REB": 4, "AST": 2, "3PM": 1}

# Score bonus per value-zone bucket. AIM and VALUE plays get the biggest
# bumps; chalk and longshots get less.
NBA_VALUE_ZONE_BONUS = {"aim": 14.0, "value": 12.0, "lean": 4.0, "longshot": 6.0}


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
    zone: str = "",
) -> str:
    """Tier the candidate by the value zone the natural line landed in plus
    supporting context (matchup, usage, minutes). Hit rate alone isn't the
    gate — the line is already chosen so probability sits near 0.50 by
    design.
    """
    usage_gate = 0.24 if market == "REB" else 0.30
    base_zones = {"aim", "value", "longshot"}
    if zone in base_zones and strong_matchup and minutes_projection >= 30 and usage_share >= usage_gate:
        return "A"
    if zone in base_zones and minutes_projection >= 28:
        return "B"
    if zone == "lean" and minutes_projection >= 28 and usage_share >= usage_gate:
        return "B"
    return "C"


def build_market_reason(
    *,
    market: str,
    model_hit_rate: float,
    implied_odds: int,
    edge: float,
    zone: str,
    usage_share: float,
    minutes_projection: float,
    matchup_ratio: float,
    is_home: bool,
    leverage_label: str,
    vs_opp_hit_rate: float,
    vs_opp_games: int,
    vs_opp_avg: float,
    opponent_abbr: str,
    projected: float,
    baseline: float,
    q4_avg: float = 0.0,
    q4_closer: bool = False,
) -> str:
    parts = [
        f"Implied {format_implied_odds(implied_odds)}",
        f"Zone {zone.upper()}",
        f"Edge {edge:+.2f}",
        f"Hit% {model_hit_rate:.0%}",
        f"Proj {projected:.1f}",
        f"Baseline {baseline:.1f}",
        f"USG {usage_share:.0%}",
        f"MIN {minutes_projection:.1f}",
        f"{market} matchup {matchup_ratio:.2f}x",
    ]
    if vs_opp_games:
        parts.append(f"H2H {vs_opp_hit_rate:.0%} vs {opponent_abbr} ({vs_opp_games}g)")
        parts.append(f"H2H avg {vs_opp_avg:.1f}")
    if is_home:
        parts.append("Home boost")
    if minutes_projection < 30:
        parts.append("Minutes watch")
    if leverage_label:
        parts.append(leverage_label)
    if q4_closer:
        parts.append(f"Q4 Closer ({q4_avg:.1f})")
    elif market == "PTS" and q4_avg >= 3.0:
        parts.append(f"Q4 {q4_avg:.1f}")
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


def parse_quarter_pts_map(plays: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Return {athlete_id: {q1,q2,q3,q4}} point totals from a game's play-by-play."""
    result: dict[str, dict[str, int]] = {}
    for play in plays:
        if not play.get("scoringPlay"):
            continue
        period = int((play.get("period") or {}).get("number") or 0)
        if period not in {1, 2, 3, 4}:
            continue
        score_value = int(play.get("scoreValue") or 0)
        if score_value <= 0:
            continue
        for participant in play.get("participants", []):
            athlete_id = str((participant.get("athlete") or {}).get("id") or "")
            if not athlete_id:
                continue
            if athlete_id not in result:
                result[athlete_id] = {"q1": 0, "q2": 0, "q3": 0, "q4": 0}
            result[athlete_id][f"q{period}"] += score_value
            break  # first participant is the scorer; skip assisters
    return result


def enrich_logs_with_quarter_data(player_log_map: dict[str, dict[str, Any]], summary_cache: dict[str, dict[str, Any]]) -> None:
    """Attach Q1-Q4 point fields to each game log where a play-by-play summary exists."""
    quarter_maps: dict[str, dict[str, dict[str, int]]] = {}
    for game_id, payload in summary_cache.items():
        if not payload:
            continue
        plays = payload.get("plays") or []
        quarter_maps[game_id] = parse_quarter_pts_map(plays)

    for profile in player_log_map.values():
        athlete_id = str(profile["player_id"])
        for log in profile["logs"]:
            q_data = quarter_maps.get(str(log.get("event_id", "")), {}).get(athlete_id, {})
            if q_data:
                log["q1_pts"] = q_data["q1"]
                log["q2_pts"] = q_data["q2"]
                log["q3_pts"] = q_data["q3"]
                log["q4_pts"] = q_data["q4"]


def quarter_avgs_from_logs(logs: list[dict[str, Any]]) -> dict[str, float | int]:
    """Compute Q1-Q4 average points from logs that have per-quarter data."""
    q_logs = [log for log in logs if "q1_pts" in log]
    if not q_logs:
        return {"q1_avg": 0.0, "q2_avg": 0.0, "q3_avg": 0.0, "q4_avg": 0.0, "q4_share": 0.0, "sample": 0}
    q1 = average(log["q1_pts"] for log in q_logs)
    q2 = average(log["q2_pts"] for log in q_logs)
    q3 = average(log["q3_pts"] for log in q_logs)
    q4 = average(log["q4_pts"] for log in q_logs)
    total = q1 + q2 + q3 + q4 or 1.0
    return {
        "q1_avg": round(q1, 2),
        "q2_avg": round(q2, 2),
        "q3_avg": round(q3, 2),
        "q4_avg": round(q4, 2),
        "q4_share": round(q4 / total, 3),
        "sample": len(q_logs),
    }
