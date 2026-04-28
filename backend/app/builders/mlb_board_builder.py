from __future__ import annotations

from collections import defaultdict

from app.builders.board_builder import sorted_candidates, to_player_row
from app.builders.universal_game_builder import empty_markets
from app.collectors.mlb_collector import collect_mlb_raw_data
from app.models.mlb_model import normalize_mlb_inputs
from app.outputs.json_writer import write_json
from app.scoring.edge_score import score_candidate
from app.utils.dates import timestamp_et


def build_mlb_board(*, config, paths) -> dict:
    raw_payload = collect_mlb_raw_data(paths.data_raw)
    games_output = []
    pinned_candidates = []

    processed_games = []
    for raw_game in raw_payload["games"]:
        candidates = [score_candidate(item) for item in normalize_mlb_inputs(raw_game)]
        processed_games.append({"raw": raw_game, "candidates": candidates})

    write_json(
        paths.data_processed / "mlb_processed.json",
        {
            "sport": "MLB",
            "date": raw_payload["date"],
            "games": [
                {
                    "game_id": game["raw"]["game_id"],
                    "matchup": f'{game["raw"]["away_team"]} @ {game["raw"]["home_team"]}',
                    "candidates": [
                        {
                            "player_id": candidate.player_id,
                            "player_name": candidate.player_name,
                            "market": candidate.market,
                            "line": candidate.line,
                            "score": candidate.score,
                            "confidence": candidate.confidence,
                            "tier": candidate.tier,
                            "reason": candidate.reason,
                        }
                        for candidate in sorted_candidates(game["candidates"])
                    ],
                }
                for game in processed_games
            ],
        },
    )

    for processed_game in processed_games:
        raw_game = processed_game["raw"]
        candidates = sorted_candidates(processed_game["candidates"])
        market_bucket = defaultdict(list)
        for candidate in candidates:
            if candidate.tier == "PASS":
                continue
            market_bucket[candidate.market].append(to_player_row(candidate))

        top_signals = [
            {
                "market": candidate.market,
                "player_name": candidate.player_name,
                "line": candidate.line,
                "score": candidate.score,
                "confidence": candidate.confidence,
                "tier": candidate.tier,
            }
            for candidate in candidates[: config.top_signals_per_game]
            if candidate.tier != "PASS"
        ]

        games_output.append(
            {
                "game_id": raw_game["game_id"],
                "matchup": f'{raw_game["away_team"]} @ {raw_game["home_team"]}',
                "time": raw_game["time"],
                "top_signals": top_signals,
                "markets": {
                    **empty_markets(),
                    **{
                        market: plays[: config.top_market_limit]
                        for market, plays in market_bucket.items()
                    },
                },
            }
        )
        pinned_candidates.extend(
            candidate for candidate in candidates if candidate.market == "HR" and candidate.tier != "PASS"
        )

    pinned_players = [to_player_row(candidate) for candidate in sorted_candidates(pinned_candidates)[:10]]
    return {
        "sport": "MLB",
        "date": raw_payload["date"],
        "last_updated": timestamp_et(),
        "pinned_board": {
            "title": "HR Top 10",
            "market": "HR",
            "players": pinned_players,
        },
        "games": games_output,
    }
