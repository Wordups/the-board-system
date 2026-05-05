from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import json
from typing import Any

import requests

from app.outputs.json_writer import write_json
from app.utils.dates import now_et, today_et


HTTP_TIMEOUT = 30
MAX_WORKERS = 8
SOCCER_MARKETS = ["GS", "AST", "OU", "ML"]
SOCCER_LEAGUES = [
    {"slug": "eng.1", "label": "Premier League"},
    {"slug": "esp.1", "label": "La Liga"},
    {"slug": "ger.1", "label": "Bundesliga"},
    {"slug": "ita.1", "label": "Serie A"},
    {"slug": "fra.1", "label": "Ligue 1"},
    {"slug": "usa.1", "label": "MLS"},
    {"slug": "uefa.champions", "label": "Champions League"},
]


def collect_soccer_raw_data(data_raw_dir: Path) -> dict[str, Any]:
    raw_path = data_raw_dir / "soccer_raw.json"
    slate_date = today_et()

    try:
        events = fetch_soccer_events(slate_date)
        if not events:
            payload = {
                "sport": "SOCCER",
                "date": slate_date.isoformat(),
                "games": [],
            }
            write_json(raw_path, payload)
            return payload

        team_keys = {
            (event["league_slug"], competitor["team"]["id"])
            for event in events
            for competitor in event["competitions"][0]["competitors"]
        }
        rosters = fetch_team_rosters(team_keys)
        recent_form_map = fetch_team_recent_form(team_keys)
        baseline = build_goal_baseline(recent_form_map)

        payload = {
            "sport": "SOCCER",
            "date": slate_date.isoformat(),
            "games": [
                build_game_payload(
                    event=event,
                    rosters=rosters,
                    recent_form_map=recent_form_map,
                    baseline=baseline,
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


def fetch_soccer_events(slate_date) -> list[dict[str, Any]]:
    events = []
    seen_ids = set()
    date_token = slate_date.strftime("%Y%m%d")
    for league in SOCCER_LEAGUES:
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league['slug']}/scoreboard"
        try:
            payload = espn_get_json(url, {"dates": date_token})
        except requests.RequestException:
            continue
        for event in payload.get("events", []):
            status = event.get("competitions", [{}])[0].get("status", {}).get("type", {})
            if status.get("completed"):
                continue
            if event["id"] in seen_ids:
                continue
            seen_ids.add(event["id"])
            events.append(
                {
                    **event,
                    "league_slug": league["slug"],
                    "league_label": league["label"],
                }
            )
    return events


def fetch_team_rosters(team_keys: set[tuple[str, str]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    def load(item: tuple[str, str]) -> tuple[tuple[str, str], list[dict[str, Any]]]:
        league_slug, team_id = item
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/teams/{team_id}/roster"
        payload = espn_get_json(url)
        return item, payload.get("athletes", [])

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, sorted(team_keys)))


def fetch_team_recent_form(team_keys: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, float]]:
    def load(item: tuple[str, str]) -> tuple[tuple[str, str], dict[str, float]]:
        league_slug, team_id = item
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/teams/{team_id}/schedule"
        payload = espn_get_json(url)
        recent = []
        for event in payload.get("events", []):
            competition = event.get("competitions", [{}])[0]
            status = competition.get("status", {}).get("type", {})
            if not status.get("completed"):
                continue
            recent.append(event)
        recent.sort(key=lambda row: row["date"], reverse=True)
        recent = recent[:5]

        points = goals_for = goals_against = 0.0
        for event in recent:
            competition = event["competitions"][0]
            competitors = competition["competitors"]
            me = next((row for row in competitors if row["team"]["id"] == team_id), None)
            opp = next((row for row in competitors if row["team"]["id"] != team_id), None)
            if not me or not opp:
                continue
            me_score = parse_int(me.get("score"))
            opp_score = parse_int(opp.get("score"))
            goals_for += me_score
            goals_against += opp_score
            if me_score > opp_score:
                points += 3.0
            elif me_score == opp_score:
                points += 1.0

        sample = max(len(recent), 1)
        return item, {
            "points_per_match": points / sample,
            "goals_for_per_match": goals_for / sample,
            "goals_against_per_match": goals_against / sample,
        }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, sorted(team_keys)))


def build_goal_baseline(recent_form_map: dict[tuple[str, str], dict[str, float]]) -> dict[str, float]:
    values = list(recent_form_map.values())
    return {
        "goals_for": average(profile["goals_for_per_match"] for profile in values) if values else 1.2,
        "goals_against": average(profile["goals_against_per_match"] for profile in values) if values else 1.2,
        "points": average(profile["points_per_match"] for profile in values) if values else 1.4,
    }


def build_game_payload(
    *,
    event: dict[str, Any],
    rosters: dict[tuple[str, str], list[dict[str, Any]]],
    recent_form_map: dict[tuple[str, str], dict[str, float]],
    baseline: dict[str, float],
) -> dict[str, Any]:
    competition = event["competitions"][0]
    competitors = competition["competitors"]
    away = next(row for row in competitors if row["homeAway"] == "away")
    home = next(row for row in competitors if row["homeAway"] == "home")
    away_team = away["team"]
    home_team = home["team"]

    away_key = (event["league_slug"], away_team["id"])
    home_key = (event["league_slug"], home_team["id"])

    candidates = []
    candidates.extend(
        build_team_player_candidates(
            game_id=event["id"],
            market_team="away",
            team=away_team,
            opponent=home_team,
            roster=rosters.get(away_key, []),
            team_form=recent_form_map.get(away_key, {}),
            opponent_form=recent_form_map.get(home_key, {}),
            baseline=baseline,
            is_home=False,
        )
    )
    candidates.extend(
        build_team_player_candidates(
            game_id=event["id"],
            market_team="home",
            team=home_team,
            opponent=away_team,
            roster=rosters.get(home_key, []),
            team_form=recent_form_map.get(home_key, {}),
            opponent_form=recent_form_map.get(away_key, {}),
            baseline=baseline,
            is_home=True,
        )
    )
    candidates.extend(
        build_match_market_candidates(
            game_id=event["id"],
            away_team=away_team,
            home_team=home_team,
            away_form=recent_form_map.get(away_key, {}),
            home_form=recent_form_map.get(home_key, {}),
            baseline=baseline,
        )
    )

    return {
        "game_id": str(event["id"]),
        "league": event["league_label"],
        "away_team": away_team["abbreviation"],
        "home_team": home_team["abbreviation"],
        "time": competition["status"]["type"].get("shortDetail") or format_event_time(competition["date"]),
        "status": {"phase": translate_state(competition["status"]["type"].get("state"))},
        "candidates": candidates,
    }


def build_team_player_candidates(
    *,
    game_id: str,
    market_team: str,
    team: dict[str, Any],
    opponent: dict[str, Any],
    roster: list[dict[str, Any]],
    team_form: dict[str, float],
    opponent_form: dict[str, float],
    baseline: dict[str, float],
    is_home: bool,
) -> list[dict[str, Any]]:
    players = []
    for athlete in roster:
        if athlete.get("status", {}).get("type") != "active":
            continue
        if athlete.get("injuries"):
            continue
        stats = flatten_soccer_stats(athlete.get("statistics", {}))
        appearances = parse_number(stats.get("appearances"))
        goals = parse_number(stats.get("totalGoals"))
        assists = parse_number(stats.get("goalAssists"))
        shots_on_target = parse_number(stats.get("shotsOnTarget"))
        total_shots = parse_number(stats.get("totalShots"))
        if appearances < 3:
            continue

        position = (athlete.get("position", {}) or {}).get("abbreviation", "")
        goals_per_match = goals / max(appearances, 1.0)
        assists_per_match = assists / max(appearances, 1.0)
        sot_per_match = shots_on_target / max(appearances, 1.0)
        shots_per_match = total_shots / max(appearances, 1.0)
        opponent_softness = normalize_rate(opponent_form.get("goals_against_per_match", baseline["goals_against"]), baseline["goals_against"] * 1.6)
        team_attack = normalize_rate(team_form.get("goals_for_per_match", baseline["goals_for"]), baseline["goals_for"] * 1.6)
        home_boost = 0.06 if is_home else 0.0

        if position in {"F", "M"} and (goals > 0 or shots_on_target > 0):
            raw_score = 100 * (
                (normalize_rate(goals_per_match, 0.9) * 0.40)
                + (normalize_rate(sot_per_match, 2.5) * 0.25)
                + (opponent_softness * 0.18)
                + (team_attack * 0.11)
                + home_boost
                + (0.06 if position == "F" else 0.0)
            )
            players.append(
                {
                    "player_id": athlete["id"],
                    "player_name": athlete["displayName"],
                    "team": team["abbreviation"],
                    "opponent": opponent["abbreviation"],
                    "game_id": str(game_id),
                    "market": "GS",
                    "line": "Anytime Goal",
                    "score": round(raw_score, 2),
                    "confidence": clamp_int(raw_score),
                    "tier": assign_soccer_tier(raw_score),
                    "reason": f"G/90 {goals_per_match:.2f} | SOT {sot_per_match:.2f} | Opp GA {opponent_form.get('goals_against_per_match', 0.0):.2f}",
                }
            )

        if position in {"F", "M", "D"} and (assists > 0 or shots_per_match > 0.8):
            raw_score = 100 * (
                (normalize_rate(assists_per_match, 0.6) * 0.42)
                + (normalize_rate(shots_per_match, 3.0) * 0.16)
                + (team_attack * 0.24)
                + (opponent_softness * 0.12)
                + home_boost
            )
            players.append(
                {
                    "player_id": athlete["id"],
                    "player_name": athlete["displayName"],
                    "team": team["abbreviation"],
                    "opponent": opponent["abbreviation"],
                    "game_id": str(game_id),
                    "market": "AST",
                    "line": "Assist",
                    "score": round(raw_score, 2),
                    "confidence": clamp_int(raw_score),
                    "tier": assign_soccer_tier(raw_score),
                    "reason": f"A/90 {assists_per_match:.2f} | Team GF {team_form.get('goals_for_per_match', 0.0):.2f} | Chance role",
                }
            )

    return [player for player in players if player["score"] >= 18]


def build_match_market_candidates(
    *,
    game_id: str,
    away_team: dict[str, Any],
    home_team: dict[str, Any],
    away_form: dict[str, float],
    home_form: dict[str, float],
    baseline: dict[str, float],
) -> list[dict[str, Any]]:
    home_strength = (
        home_form.get("points_per_match", baseline["points"]) * 0.50
        + home_form.get("goals_for_per_match", baseline["goals_for"]) * 0.30
        - home_form.get("goals_against_per_match", baseline["goals_against"]) * 0.15
        + 0.20
    )
    away_strength = (
        away_form.get("points_per_match", baseline["points"]) * 0.50
        + away_form.get("goals_for_per_match", baseline["goals_for"]) * 0.30
        - away_form.get("goals_against_per_match", baseline["goals_against"]) * 0.15
    )
    if home_strength >= away_strength:
        ml_team = home_team["abbreviation"]
        ml_opp = away_team["abbreviation"]
        ml_score = normalize_rate(home_strength - away_strength + 1.0, 2.5) * 100
        ml_reason = f"Home form {home_form.get('points_per_match', 0.0):.2f} PPM | Away {away_form.get('points_per_match', 0.0):.2f} PPM"
    else:
        ml_team = away_team["abbreviation"]
        ml_opp = home_team["abbreviation"]
        ml_score = normalize_rate(away_strength - home_strength + 1.0, 2.5) * 100
        ml_reason = f"Away form {away_form.get('points_per_match', 0.0):.2f} PPM | Home {home_form.get('points_per_match', 0.0):.2f} PPM"

    home_attack = home_form.get("goals_for_per_match", baseline["goals_for"])
    away_attack = away_form.get("goals_for_per_match", baseline["goals_for"])
    home_defense = home_form.get("goals_against_per_match", baseline["goals_against"])
    away_defense = away_form.get("goals_against_per_match", baseline["goals_against"])

    total_goal_signal = (
        ((home_attack + away_defense) / 2.0)
        + ((away_attack + home_defense) / 2.0)
    )
    both_teams_push = min(home_attack, away_attack)
    defensive_drag = max(0.0, (baseline["goals_against"] * 2.0) - (home_defense + away_defense))
    attack_bias = (home_attack + away_attack) - (baseline["goals_for"] * 2.0)
    adjusted_total = total_goal_signal + (attack_bias * 0.18) - (defensive_drag * 0.10)

    goal_delta = adjusted_total - 2.55
    if goal_delta >= 0.0:
        ou_line = "Over 2.5 Goals"
        ou_score = clamp_score(52.0 + goal_delta * 24.0 + normalize_rate(both_teams_push, baseline["goals_for"] * 1.4) * 9.0)
    else:
        ou_line = "Under 2.5 Goals"
        ou_score = clamp_score(52.0 + abs(goal_delta) * 24.0 + normalize_rate(defensive_drag, baseline["goals_against"] * 1.6) * 8.0)

    return [
        {
            "player_id": f"{game_id}-ml",
            "player_name": ml_team,
            "team": ml_team,
            "opponent": ml_opp,
            "game_id": str(game_id),
            "market": "ML",
            "line": "Moneyline",
            "score": round(ml_score, 2),
            "confidence": clamp_int(ml_score),
            "tier": assign_soccer_tier(ml_score),
            "reason": ml_reason,
        },
        {
            "player_id": f"{game_id}-ou",
            "player_name": "Match Total",
            "team": home_team["abbreviation"],
            "opponent": away_team["abbreviation"],
            "game_id": str(game_id),
            "market": "OU",
            "line": ou_line,
            "score": round(ou_score, 2),
            "confidence": clamp_int(ou_score),
            "tier": assign_soccer_tier(ou_score),
            "reason": f"Adj goal profile {adjusted_total:.2f} | BTTS push {both_teams_push:.2f}",
        },
    ]


def flatten_soccer_stats(stat_blob: dict[str, Any]) -> dict[str, float]:
    flat: dict[str, float] = {}
    for category in stat_blob.get("splits", {}).get("categories", []):
        for stat in category.get("stats", []):
            flat[stat["name"]] = parse_number(stat.get("value"))
    return flat


def assign_soccer_tier(score: float) -> str:
    if score >= 72:
        return "A"
    if score >= 56:
        return "B"
    return "C"


def translate_state(state: str | None) -> str:
    if state == "post":
        return "final"
    if state == "in":
        return "live"
    return "pregame"


def format_event_time(value: str) -> str:
    event_time = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(now_et().tzinfo)
    return event_time.strftime("%I:%M %p ET").lstrip("0")


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


def parse_int(value: Any) -> int:
    if isinstance(value, dict):
        return parse_int(value.get("value") if "value" in value else value.get("displayValue"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def average(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def normalize_rate(value: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return max(0.0, min(value / scale, 1.0))


def clamp_int(value: float) -> int:
    return max(1, min(99, round(value)))


def clamp_score(value: float) -> float:
    return max(1.0, min(99.0, value))
