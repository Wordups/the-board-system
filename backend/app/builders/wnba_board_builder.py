from __future__ import annotations

from collections import defaultdict
import json

from app.builders.nba_board_builder import (
    apply_anti_correlation,
    build_game_clusters,
    build_hero_pick,
    build_pick_of_day,
    build_section_boards,
    to_board_row,
)
from app.builders.universal_game_builder import empty_markets_for
from app.collectors.wnba_collector import WNBA_MARKETS, collect_wnba_raw_data
from app.outputs.json_writer import write_json
from app.sim.edge import build_sim_board
from app.sim.sim_engine import sim_prob_pct, simulate_candidates
from app.utils.dates import timestamp_et


def build_wnba_board(*, config, paths) -> dict:
    raw_payload = collect_wnba_raw_data(paths.data_raw)
    previous_pick = load_previous_pick(paths)
    games_output = []
    pinned_candidates = []
    processed_games = []
    all_candidates = []

    for raw_game in raw_payload["games"]:
        candidates = [candidate for candidate in raw_game["candidates"] if candidate["score"] > 0]
        candidates = apply_anti_correlation(candidates)
        processed_games.append({"raw": raw_game, "candidates": candidates})
        all_candidates.extend(candidates)

    simulate_candidates(all_candidates, sport="WNBA")
    pick_of_day = build_pick_of_day(processed_games, previous_pick, sport="WNBA")
    game_clusters = build_game_clusters(processed_games)
    section_boards = build_section_boards(all_candidates, config.top_market_limit)
    hero_pick = build_hero_pick(pick_of_day, all_candidates, sport="WNBA")

    write_json(
        paths.data_processed / "wnba_processed.json",
        {
            "sport": "WNBA",
            "date": raw_payload["date"],
            "season_type": raw_payload.get("season_type"),
            "pick_of_day": pick_of_day,
            "games": processed_games,
        },
    )

    for processed_game in processed_games:
        raw_game = processed_game["raw"]
        candidates = sorted(processed_game["candidates"], key=lambda row: (row["score"], row["confidence"]), reverse=True)
        market_bucket = defaultdict(list)
        for candidate in candidates:
            market_bucket[candidate["market"]].append(to_board_row(candidate))

        top_signals = [
            {
                "market": candidate["market"],
                "player_id": str(candidate["player_id"]),
                "player_name": candidate["player_name"],
                "line": candidate["line"],
                "score": candidate["score"],
                "confidence": candidate["confidence"],
                "tier": candidate["tier"],
                "sim_prob_pct": sim_prob_pct(candidate),
            }
            for candidate in candidates[: config.top_signals_per_game]
        ]

        games_output.append(
            {
                "game_id": raw_game["game_id"],
                "matchup": f'{raw_game["away_team"]} @ {raw_game["home_team"]}',
                "time": raw_game["time"],
                "top_signals": top_signals,
                "markets": {
                    **empty_markets_for(WNBA_MARKETS),
                    **{market: rows[: config.top_market_limit] for market, rows in market_bucket.items()},
                },
            }
        )
        pinned_candidates.extend(candidate for candidate in candidates if candidate["market"] == "PTS")

    pinned_players = [
        to_board_row(candidate)
        for candidate in sorted(pinned_candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True)[:10]
    ]

    # Best Available: top A-tier candidates across every market, dedup'd by
    # (player, market) so the same player doesn't take three rows. Sits
    # next to PTS Top 10 as the cross-market featured pool.
    best_available_seen = set()
    best_available_pool = []
    for candidate in sorted(all_candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True):
        if candidate.get("tier") != "A":
            continue
        key = (candidate["player_id"], candidate["market"])
        if key in best_available_seen:
            continue
        best_available_seen.add(key)
        best_available_pool.append(candidate)
        if len(best_available_pool) >= 10:
            break
    best_available_players = [to_board_row(candidate) for candidate in best_available_pool]

    return {
        "sport": "WNBA",
        "date": raw_payload["date"],
        "last_updated": timestamp_et(),
        "hero_pick": hero_pick,
        "game_clusters": game_clusters,
        "section_boards": section_boards,
        "pinned_board": {
            "title": "PTS Top 10",
            "market": "PTS",
            "players": pinned_players,
        },
        "best_available_board": {
            "title": "Best Available",
            "subtitle": "A-tier across every market",
            "players": best_available_players,
        },
        "sim_board": build_sim_board(all_candidates, sport="WNBA"),
        "games": games_output,
    }


def load_previous_pick(paths) -> dict | None:
    previous_path = paths.data_processed / "wnba_processed.json"
    if not previous_path.exists():
        return None
    try:
        payload = json.loads(previous_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload.get("pick_of_day")
