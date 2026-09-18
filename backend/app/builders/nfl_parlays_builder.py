from __future__ import annotations

from typing import Any
from app.data.fanduel_scraper import FanDuelScraper
from app.sim.nfl_parlays import build_same_game_parlays_for_game
from app.utils.dates import timestamp_et


def build_nfl_parlays_board_from_fanduel(*, games: list[dict[str, str]]) -> dict:
    """Build parlay board directly from FanDuel props data.

    Args:
        games: List of {"game_id": str, "matchup": str} for FanDuel

    Returns:
        {
            "sport": "NFL",
            "date": str,
            "last_updated": str,
            "parlays": [...],
            "td_parlays": [...]
        }
    """
    scraper = FanDuelScraper()
    parlays = []
    td_parlays = []

    for game in games:
        try:
            props_data = scraper.fetch_game_props(
                game_id=game["game_id"],
                matchup=game["matchup"]
            )

            if not props_data.get("candidates"):
                continue

            ticket = build_same_game_parlays_for_game(
                game_id=props_data["game_id"],
                matchup=props_data["matchup"],
                time=game.get("time", ""),
                candidates=props_data["candidates"],
            )

            if ticket:
                parlays.append(ticket)
                # Extract TD parlay if present
                if "td_parlay" in ticket:
                    td_parlays.append({
                        "game_id": ticket["game_id"],
                        "matchup": ticket["matchup"],
                        "time": ticket.get("time", ""),
                        "game_script": ticket["game_script"],
                        "td_parlay": ticket["td_parlay"],
                    })
        except Exception as e:
            print(f"Error processing {game['matchup']}: {e}")
            continue

    return {
        "sport": "NFL",
        "date": timestamp_et().split("T")[0],
        "last_updated": timestamp_et(),
        "parlays": parlays,
        "td_parlays": td_parlays,
        "source": "FanDuel live props",
    }


def build_nfl_parlays_board(*, config, paths) -> dict:
    """Fallback builder using ESPN data."""
    from app.collectors.nfl_collector import collect_nfl_raw_data

    raw_payload = collect_nfl_raw_data(paths.data_raw)
    parlays = []
    td_parlays = []

    for raw_game in raw_payload["games"]:
        ticket = build_same_game_parlays_for_game(
            game_id=raw_game["game_id"],
            matchup=f'{raw_game["away_team"]} @ {raw_game["home_team"]}',
            time=raw_game.get("time", ""),
            candidates=raw_game["candidates"],
        )
        if ticket:
            parlays.append(ticket)
            # Extract TD parlay if present
            if "td_parlay" in ticket:
                td_parlays.append({
                    "game_id": ticket["game_id"],
                    "matchup": ticket["matchup"],
                    "time": ticket.get("time", ""),
                    "game_script": ticket["game_script"],
                    "td_parlay": ticket["td_parlay"],
                })

    week = raw_payload.get("week")
    season = raw_payload.get("season")
    week_label = f"Week {week} · {season}" if week and season else "Week 1"

    return {
        "sport": "NFL",
        "date": raw_payload["date"],
        "last_updated": timestamp_et(),
        "week": week,
        "season": season,
        "uncertainty_note": f"{week_label} — built entirely from 2025 prior-season stats; no current-season sample yet.",
        "parlays": parlays,
        "td_parlays": td_parlays,
    }
