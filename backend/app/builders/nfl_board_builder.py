from __future__ import annotations

from collections import defaultdict

from app.builders.universal_game_builder import empty_markets_for
from app.collectors.nfl_collector import NFL_MARKETS, collect_nfl_raw_data
from app.utils.dates import timestamp_et


def build_nfl_top_signals(candidates: list[dict], limit: int) -> list[dict]:
    preferred_markets = ("TD", "ML", "RushYds", "RecYds", "REC", "PassTD")
    selected: list[dict] = []
    used_players: set[str] = set()

    for market in preferred_markets:
        market_rows = [
            candidate for candidate in candidates
            if candidate["market"] == market and candidate["player_name"] not in used_players
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


def build_nfl_board(*, config, paths) -> dict:
    raw_payload = collect_nfl_raw_data(paths.data_raw)
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
                "time": raw_game["time"],
                "top_signals": build_nfl_top_signals(ranked, config.top_signals_per_game),
                "markets": {
                    **empty_markets_for(NFL_MARKETS),
                    **{market: rows[: config.top_market_limit] for market, rows in market_bucket.items()},
                },
            }
        )
        pinned_candidates.extend(candidate for candidate in ranked if candidate["market"] == "TD")

    week = raw_payload.get("week")
    season = raw_payload.get("season")
    week_label = f"Week {week} · {season}" if week and season else "Week 1"

    return {
        "sport": "NFL",
        "date": raw_payload["date"],
        "last_updated": timestamp_et(),
        "week": week,
        "season": season,
        # Week 1 has zero current-season sample and August depth charts are
        # still settling — surfaced here the same way the existing stale-data
        # banner works, rather than presenting Week 1 grades with the same
        # implied confidence as a mid-season slate. Every player row also
        # carries lineup_confirmed: false for the same reason.
        "uncertainty_note": f"{week_label} — built entirely from 2025 prior-season stats; no current-season sample yet, and rosters/depth charts are not yet final.",
        "pinned_board": {
            "title": "Anytime TD Top 10",
            "market": "TD",
            "players": [to_board_row(candidate) for candidate in sorted(pinned_candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True)[:10]],
        },
        "games": games_output,
    }


def to_board_row(candidate: dict) -> dict:
    row = {
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
    for key in ("sim_prob_pct", "model_hit_rate_raw", "implied_odds", "lineup_confirmed"):
        if key in candidate:
            row[key] = candidate[key]
    return row
