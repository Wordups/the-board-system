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
MAX_RANK_FALLBACK = 250


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
            if competition.get("status", {}).get("type", {}).get("completed"):
                continue
            same_day.append({**competition, "grouping": grouping.get("grouping", {})})

    player_profiles = build_player_profiles(tournament, slate_date.year)
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


def build_player_profiles(tournament: dict[str, Any], season_year: int) -> dict[str, dict[str, Any]]:
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
                        "rank_uncertain": current_rank(competitor) >= MAX_RANK_FALLBACK,
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

    resolved_ranks = fetch_player_rank_map(
        tournament.get("tour_slug", "").lower(),
        season_year,
        profiles.keys(),
    )
    for player_id, rank_value in resolved_ranks.items():
        if player_id in profiles and rank_value:
            profiles[player_id]["rank"] = rank_value
            profiles[player_id]["rank_uncertain"] = rank_value >= MAX_RANK_FALLBACK
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

    form_gap = favorite_form - underdog_form
    rank_signal = max(0.0, min(rank_gap / 85.0, 0.34))
    form_signal = max(-0.14, min(form_gap * 0.22, 0.14))
    sample_penalty = 0.04 if favorite["wins"] + favorite["losses"] < 2 or underdog["wins"] + underdog["losses"] < 2 else 0.0
    rank_penalty = 0.09 if favorite.get("rank_uncertain") or underdog.get("rank_uncertain") else 0.0
    ml_score = 100 * min(0.9, max(0.48, 0.54 + rank_signal + form_signal - sample_penalty - rank_penalty))
    ml_reason = (
        f"Rank {favorite['rank']} vs {underdog['rank']} | "
        f"Form {favorite['wins']}-{favorite['losses']} vs {underdog['wins']}-{underdog['losses']}"
        f"{' | Rank est.' if rank_penalty else ''}"
    )

    close_match = rank_gap <= 16 and abs(form_gap) <= 0.14
    favorite_clear = rank_gap >= 28 or form_gap >= 0.18
    if best_of == 5:
        if close_match:
            ou_line = "Over 36.5 Games"
            ou_score = 100 * min(0.8, 0.62 + min(rank_gap / 160.0, 0.05) + max(0.0, 0.10 - abs(form_gap)) - rank_penalty)
            sets_line = "Over 3.5 Sets"
            sets_score = 100 * min(0.77, 0.6 + min(rank_gap / 150.0, 0.04) - rank_penalty)
        elif favorite_clear:
            ou_line = "Under 34.5 Games"
            ou_score = 100 * min(0.78, 0.58 + min(rank_gap / 120.0, 0.12) + max(form_gap, 0.0) * 0.1 - rank_penalty)
            sets_line = f"{favorite['player_name']} -1.5 Sets"
            sets_score = 100 * min(0.82, 0.64 + min(rank_gap / 95.0, 0.16) + max(form_gap, 0.0) * 0.12 - rank_penalty)
        else:
            ou_line = "Over 35.5 Games"
            ou_score = 100 * max(0.5, 0.59 - rank_penalty)
            sets_line = "Over 3.5 Sets"
            sets_score = 100 * max(0.5, 0.57 - rank_penalty)
    else:
        if close_match:
            ou_line = "Over 22.5 Games"
            ou_score = 100 * min(0.77, 0.63 + min(rank_gap / 140.0, 0.05) + max(0.0, 0.10 - abs(form_gap)) - rank_penalty)
            sets_line = "Over 2.5 Sets"
            sets_score = 100 * min(0.74, 0.59 + min(rank_gap / 160.0, 0.04) - rank_penalty)
        elif favorite_clear:
            ou_line = "Under 21.5 Games"
            ou_score = 100 * min(0.75, 0.57 + min(rank_gap / 115.0, 0.12) + max(form_gap, 0.0) * 0.1 - rank_penalty)
            sets_line = f"{favorite['player_name']} -1.5 Sets"
            sets_score = 100 * min(0.8, 0.63 + min(rank_gap / 90.0, 0.16) + max(form_gap, 0.0) * 0.12 - rank_penalty)
        else:
            ou_line = "Over 21.5 Games"
            ou_score = 100 * max(0.5, 0.58 - rank_penalty)
            sets_line = "Over 2.5 Sets"
            sets_score = 100 * max(0.5, 0.56 - rank_penalty)

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
            "reason": f"Round {competition.get('round', {}).get('displayName', '')} | Rank gap {rank_gap} | Form gap {form_gap:+.2f}",
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
            "reason": f"Best-of-{best_of} | Sets {favorite['sets_won']}-{favorite['sets_lost']} | Rank gap {rank_gap}",
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


def fetch_player_rank_map(tour_slug: str, season_year: int, player_ids) -> dict[str, int]:
    if tour_slug not in {"atp", "wta"}:
        return {}

    rank_map: dict[str, int] = {}
    session = requests.Session()
    session.headers.update({"User-Agent": "the-board-system/1.0"})
    for player_id in sorted({str(pid) for pid in player_ids}):
        base_url = (
            f"http://sports.core.api.espn.com/v2/sports/tennis/leagues/{tour_slug}/"
            f"seasons/{season_year}/players/{player_id}/ranks?lang=en&region=us"
        )
        try:
            index_payload = session.get(base_url, timeout=HTTP_TIMEOUT).json()
            items = index_payload.get("items", [])
            if not items:
                continue
            ref = items[0].get("$ref")
            if not ref:
                continue
            rank_payload = session.get(ref, timeout=HTTP_TIMEOUT).json()
            current_rank = (
                rank_payload.get("rank", {}).get("current")
                or rank_payload.get("rank", {}).get("value")
            )
            if current_rank:
                rank_map[player_id] = int(current_rank)
        except Exception:
            continue
    return rank_map


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
        rank = competitor.get("tournamentSeed")
    if rank is None:
        return MAX_RANK_FALLBACK
    try:
        return int(rank)
    except (TypeError, ValueError):
        return MAX_RANK_FALLBACK


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
