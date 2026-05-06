from __future__ import annotations

from collections import defaultdict

from app.builders.universal_game_builder import empty_markets_for
from app.collectors.soccer_collector import SOCCER_MARKETS, collect_soccer_raw_data
from app.utils.dates import timestamp_et


def build_soccer_top_signals(candidates: list[dict], limit: int) -> list[dict]:
    preferred_markets = ("GS", "AST", "OU", "ML")
    selected: list[dict] = []
    used_players: set[str] = set()

    for market in preferred_markets:
        market_rows = [
            candidate for candidate in candidates
            if candidate["market"] == market
            and candidate["player_name"] not in used_players
            and (market not in {"OU", "ML"} or candidate["tier"] in {"A", "B"})
        ]
        if not market_rows:
            continue
        best = market_rows[0]
        selected.append(best)
        used_players.add(best["player_name"])
        if len(selected) >= limit:
            break

    if len(selected) < limit:
        for candidate in candidates:
            if candidate["player_name"] in used_players:
                continue
            if candidate["market"] in {"OU", "ML"} and candidate["tier"] not in {"A", "B"}:
                continue
            selected.append(candidate)
            used_players.add(candidate["player_name"])
            if len(selected) >= limit:
                break

    return [
        {
            "market": candidate["market"],
            "player_name": candidate["player_name"],
            "line": candidate["line"],
            "score": candidate["score"],
            "confidence": candidate["confidence"],
            "tier": candidate["tier"],
            "player_id": str(candidate["player_id"]),
        }
        for candidate in selected
    ]


def build_soccer_board(*, config, paths) -> dict:
    raw_payload = collect_soccer_raw_data(paths.data_raw)
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
                "matchup": f'{raw_game["away_team"]} @ {raw_game["home_team"]}',
                "time": f'{raw_game["league"]} | {raw_game["time"]}',
                "top_signals": build_soccer_top_signals(ranked, config.top_signals_per_game),
                "markets": {
                    **empty_markets_for(SOCCER_MARKETS),
                    **{market: rows[: config.top_market_limit] for market, rows in market_bucket.items()},
                },
            }
        )
        pinned_candidates.extend(candidate for candidate in ranked if candidate["market"] == "GS")

    return {
        "sport": "SOCCER",
        "date": raw_payload["date"],
        "last_updated": timestamp_et(),
        "pinned_board": {
            "title": "Anytime Goalscorer Top 10",
            "market": "GS",
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
