from __future__ import annotations

from app.collectors.nfl_collector import collect_nfl_raw_data
from app.sim.nfl_parlays import build_same_game_parlays_for_game
from app.utils.dates import timestamp_et


def build_nfl_parlays_board(*, config, paths) -> dict:
    raw_payload = collect_nfl_raw_data(paths.data_raw)
    same_game_parlays = []

    for raw_game in raw_payload["games"]:
        ticket = build_same_game_parlays_for_game(
            game_id=raw_game["game_id"],
            matchup=f'{raw_game["away_team"]} @ {raw_game["home_team"]}',
            time=raw_game.get("time"),
            candidates=raw_game["candidates"],
        )
        if ticket:
            same_game_parlays.append(ticket)

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
        "parlays": same_game_parlays,
    }
