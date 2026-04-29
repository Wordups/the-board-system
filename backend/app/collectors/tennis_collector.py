from __future__ import annotations

from pathlib import Path
import json
from typing import Any

import requests

from app.outputs.json_writer import write_json
from app.utils.dates import now_et, today_et


HTTP_TIMEOUT = 30
TENNIS_MARKETS = ["ML", "O/U", "Sets"]
TENNIS_TOURS = [
    {"slug": "atp", "label": "ATP"},
    {"slug": "wta", "label": "WTA"},
]


def collect_tennis_raw_data(data_raw_dir: Path) -> dict[str, Any]:
    raw_path = data_raw_dir / "tennis_raw.json"
    slate_date = today_et()

    try:
        tournaments = fetch_tennis_tournaments(slate_date)
        games = []
        for tournament in tournaments:
            games.extend(build_matches_for_tournament(tournament, slate_date))
        payload = {
            "sport": "TENNIS",
            "date": slate_date.isoformat(),
            "games": games,
        }
        write_json(raw_path, payload)
        return payload
    except Exception:
        if raw_path.exists():
            return json.loads(raw_path.read_text(encoding="utf-8"))
        raise


def fetch_tennis_tournaments(slate_date) -> list[dict[str, Any]]:
    date_token = slate_date.strftime("%Y%m%d")
    tournaments = []
    for tour in TENNIS_TOURS:
        url = f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour['slug']}/scoreboard"
        payload = espn_get_json(url, {"dates": date_token})
        for event in payload.get("events", []):
            tournaments.append({**event, "tour": tour["label"], "tour_slug": tour["slug"]})
    return tournaments


def build_matches_for_tournament(tournament: dict[str, Any], slate_date) -> list[dict[str, Any]]:
    same_day = []
    for grouping in tournament.get("groupings", []):
        for competition in grouping.get("competitions", []):
            if competition["date"][:10] != slate_date.isoformat():
                continue
            same_day.append({**competition, "grouping": grouping.get("grouping", {})})

    player_profiles = build_player_profiles(tournament)
    games = []
    for competition in same_day:
        if len(competition.get("competitors", [])) != 2:
            continue
        competitor_a = competition["competitors"][0]
        competitor_b = competition["competitors"][1]
        if not is_named_competitor(competitor_a) or not is_named_competitor(competitor_b):
            continue
        candidates = build_match_candidates(
            competition=competition,
            tournament=tournament,
            player_a=player_profiles.get(str(competitor_a["id"]), default_profile(competitor_a)),
            player_b=player_profiles.get(str(competitor_b["id"]), default_profile(competitor_b)),
        )
        games.append(
            {
                "game_id": str(competition["id"]),
                "tournament": tournament["name"],
                "tour": tournament["tour"],
                "round": competition.get("round", {}).get("displayName", ""),
                "player_a": competitor_name(competitor_a),
                "player_b": competitor_name(competitor_b),
                "time": competition["status"]["type"].get("shortDetail") or format_event_time(competition["date"]),
                "status": {"phase": translate_state(competition["status"]["type"].get("state"))},
                "candidates": candidates,
            }
        )
    return games


def build_player_profiles(tournament: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    for grouping in tournament.get("groupings", []):
        for competition in grouping.get("competitions", []):
            for competitor in competition.get("competitors", []):
                player_id = str(competitor["id"])
                profile = profiles.setdefault(
                    player_id,
                    {
                        "player_id": player_id,
                        "player_name": competitor_name(competitor),
                        "rank": current_rank(competitor),
                        "wins": 0,
                        "losses": 0,
                        "sets_won": 0,
                        "sets_lost": 0,
                    },
                )
                profile["rank"] = min(profile["rank"], current_rank(competitor))
                if competition.get("status", {}).get("type", {}).get("completed"):
                    if competitor.get("winner"):
                        profile["wins"] += 1
                    else:
                        profile["losses"] += 1
                    for line in competitor.get("linescores", []):
                        if line.get("winner"):
                            profile["sets_won"] += 1
                        else:
                            profile["sets_lost"] += 1
    return profiles


def build_match_candidates(
    *,
    competition: dict[str, Any],
    tournament: dict[str, Any],
    player_a: dict[str, Any],
    player_b: dict[str, Any],
) -> list[dict[str, Any]]:
    favorite, underdog = choose_favorite(player_a, player_b)
    rank_gap = abs(player_a["rank"] - player_b["rank"])
    favorite_form = player_form_score(favorite)
    underdog_form = player_form_score(underdog)
    format_periods = competition.get("format", {}).get("regulation", {}).get("periods", 3)
    best_of = 5 if format_periods >= 5 else 3

    ml_score = 100 * min(0.95, 0.42 + rank_gap / 80.0 + (favorite_form - underdog_form) * 0.18)
    ml_reason = f"Rank {favorite['rank']} vs {underdog['rank']} | Tournament form {favorite['wins']}-{favorite['losses']}"

    close_match = rank_gap <= 18 and abs(favorite_form - underdog_form) <= 0.18
    if best_of == 5:
        ou_line = "Over 36.5 Games" if close_match else "Under 34.5 Games"
        sets_line = f"{favorite['player_name']} -1.5 Sets" if rank_gap >= 12 else "Over 3.5 Sets"
    else:
        ou_line = "Over 22.5 Games" if close_match else "Under 21.5 Games"
        sets_line = f"{favorite['player_name']} -1.5 Sets" if rank_gap >= 12 else "Over 2.5 Sets"

    ou_score = 100 * (0.66 if close_match else 0.58 + min(rank_gap / 120.0, 0.12))
    sets_score = 100 * (0.63 + min(rank_gap / 100.0, 0.18) + max(favorite_form - underdog_form, 0.0) * 0.10)

    matchup = f"{player_a['player_name']} vs {player_b['player_name']}"
    return [
        {
            "player_id": favorite["player_id"],
            "player_name": favorite["player_name"],
            "team": tournament["tour"],
            "opponent": underdog["player_name"],
            "game_id": str(competition["id"]),
            "market": "ML",
            "line": "Match Winner",
            "score": round(ml_score, 2),
            "confidence": clamp_int(ml_score),
            "tier": assign_tennis_tier(ml_score),
            "reason": f"{ml_reason} | {matchup}",
        },
        {
            "player_id": f"{competition['id']}-ou",
            "player_name": "Match Total",
            "team": tournament["tour"],
            "opponent": matchup,
            "game_id": str(competition["id"]),
            "market": "O/U",
            "line": ou_line,
            "score": round(ou_score, 2),
            "confidence": clamp_int(ou_score),
            "tier": assign_tennis_tier(ou_score),
            "reason": f"Round {competition.get('round', {}).get('displayName', '')} | Rank gap {rank_gap}",
        },
        {
            "player_id": f"{competition['id']}-sets",
            "player_name": favorite["player_name"],
            "team": tournament["tour"],
            "opponent": underdog["player_name"],
            "game_id": str(competition["id"]),
            "market": "Sets",
            "line": sets_line,
            "score": round(sets_score, 2),
            "confidence": clamp_int(sets_score),
            "tier": assign_tennis_tier(sets_score),
            "reason": f"Best-of-{best_of} | Tournament sets {favorite['sets_won']}-{favorite['sets_lost']}",
        },
    ]


def choose_favorite(player_a: dict[str, Any], player_b: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    score_a = (200 - player_a["rank"]) * 0.70 + player_form_score(player_a) * 30
    score_b = (200 - player_b["rank"]) * 0.70 + player_form_score(player_b) * 30
    return (player_a, player_b) if score_a >= score_b else (player_b, player_a)


def player_form_score(player: dict[str, Any]) -> float:
    matches = max(player["wins"] + player["losses"], 1)
    win_rate = player["wins"] / matches
    sets_margin = max(player["sets_won"] - player["sets_lost"], 0) / max(player["sets_won"] + player["sets_lost"], 1)
    return min(1.0, win_rate * 0.72 + sets_margin * 0.28)


def default_profile(competitor: dict[str, Any]) -> dict[str, Any]:
    return {
        "player_id": str(competitor["id"]),
        "player_name": competitor_name(competitor),
        "rank": current_rank(competitor),
        "wins": 0,
        "losses": 0,
        "sets_won": 0,
        "sets_lost": 0,
    }


def competitor_name(competitor: dict[str, Any]) -> str:
    athlete_name = competitor.get("athlete", {}).get("displayName")
    if athlete_name:
        return athlete_name
    roster_name = competitor.get("roster", {}).get("displayName")
    if roster_name:
        return roster_name
    athletes = competitor.get("roster", {}).get("athletes", [])
    names = [athlete.get("displayName") for athlete in athletes if athlete.get("displayName")]
    if names:
        return " / ".join(names)
    return "Unknown"


def is_named_competitor(competitor: dict[str, Any]) -> bool:
    name = competitor_name(competitor)
    return bool(name and name != "Unknown")


def current_rank(competitor: dict[str, Any]) -> int:
    rank = competitor.get("curatedRank", {}).get("current")
    if rank is None:
        return 250
    try:
        return int(rank)
    except (TypeError, ValueError):
        return 250


def assign_tennis_tier(score: float) -> str:
    if score >= 74:
        return "A"
    if score >= 58:
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


def clamp_int(value: float) -> int:
    return max(1, min(99, round(value)))
