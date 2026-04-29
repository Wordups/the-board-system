from __future__ import annotations

from collections import defaultdict

from app.builders.universal_game_builder import empty_markets_for
from app.collectors.tennis_collector import TENNIS_MARKETS, collect_tennis_raw_data
from app.utils.dates import timestamp_et


def build_tennis_board(*, config, paths) -> dict:
    raw_payload = collect_tennis_raw_data(paths.data_raw)
    games_output = []
    pinned_candidates = []

    for raw_game in raw_payload["games"]:
        ranked = sorted(raw_game["candidates"], key=lambda row: (row["score"], row["confidence"]), reverse=True)
        market_bucket = defaultdict(list)
        for candidate in ranked:
            market_bucket[candidate["market"]].append(to_board_row(candidate))

        games_output.append(
            {
                "game_id": raw_game["game_id"],
                "matchup": f'{raw_game["player_a"]} vs {raw_game["player_b"]}',
                "time": f'{raw_game["tour"]} | {raw_game["tournament"]} | {raw_game["time"]}',
                "top_signals": [
                    {
                        "market": candidate["market"],
                        "player_name": candidate["player_name"],
                        "line": candidate["line"],
                        "score": candidate["score"],
                        "confidence": candidate["confidence"],
                        "tier": candidate["tier"],
                        "player_id": str(candidate["player_id"]),
                    }
                    for candidate in ranked[: config.top_signals_per_game]
                ],
                "markets": {
                    **empty_markets_for(TENNIS_MARKETS),
                    **{market: rows[: config.top_market_limit] for market, rows in market_bucket.items()},
                },
            }
        )
        pinned_candidates.extend(candidate for candidate in ranked if candidate["market"] == "ML")

    return {
        "sport": "TENNIS",
        "date": raw_payload["date"],
        "last_updated": timestamp_et(),
        "pinned_board": {
            "title": "Match Win Top 10",
            "market": "ML",
            "players": [to_board_row(candidate) for candidate in sorted(pinned_candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True)[:10]],
        },
        "games": games_output,
    }


def to_board_row(candidate: dict) -> dict:
    return {
        "player_id": str(candidate["player_id"]),
        "player_name": candidate["player_name"],
        "team": candidate["team"],
        "opponent": candidate["opponent"],
        "line": candidate["line"],
        "score": round(float(candidate["score"]), 2),
        "confidence": int(candidate["confidence"]),
        "tier": candidate["tier"],
        "reason": candidate["reason"],
    }
