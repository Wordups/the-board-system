from __future__ import annotations

from collections import defaultdict
import json

from app.builders.board_builder import sorted_candidates, to_player_row
from app.builders.universal_game_builder import empty_markets
from app.collectors.mlb_collector import collect_mlb_raw_data
from app.models.mlb_model import normalize_mlb_inputs
from app.outputs.json_writer import write_json
from app.scoring.edge_score import score_candidate
from app.scoring.confidence import to_confidence
from app.scoring.tiers import assign_tier
from app.utils.dates import timestamp_et


def build_mlb_board(*, config, paths) -> dict:
    raw_payload = collect_mlb_raw_data(paths.data_raw)
    games_output = []
    pinned_candidates = []
    game_status_by_id = {}

    processed_games = []
    for raw_game in raw_payload["games"]:
        candidates = [score_candidate(item) for item in normalize_mlb_inputs(raw_game)]
        processed_games.append({"raw": raw_game, "candidates": candidates})
        game_status_by_id[raw_game["game_id"]] = raw_game.get("status", {})

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

    previous_pinned_players = load_previous_pinned_players(paths)
    pinned_players = build_sticky_hr_board(
        candidates=sorted_candidates(pinned_candidates),
        previous_pinned_players=previous_pinned_players,
        game_status_by_id=game_status_by_id,
    )
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


def load_previous_pinned_players(paths) -> dict[str, dict]:
    previous_path = paths.data_final / "mlb.json"
    if not previous_path.exists():
        return {}
    try:
        payload = json.loads(previous_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    players = payload.get("pinned_board", {}).get("players", [])
    return {
        str(player["player_id"]): player
        for player in players
        if player.get("player_id")
    }


def build_sticky_hr_board(*, candidates, previous_pinned_players, game_status_by_id) -> list[dict]:
    sticky_rows = []
    for candidate in candidates:
        status = game_status_by_id.get(candidate.game_id, {})
        previous = previous_pinned_players.get(str(candidate.player_id))
        sticky_score = apply_hr_board_sliding_scale(
            base_score=candidate.score,
            previous_score=(previous or {}).get("score"),
            status=status,
        )
        if sticky_score <= 0:
            continue
        sticky_rows.append(
            {
                "player_id": candidate.player_id,
                "player_name": candidate.player_name,
                "team": candidate.team,
                "opponent": candidate.opponent,
                "line": candidate.line,
                "score": sticky_score,
                "confidence": to_confidence(sticky_score),
                "tier": assign_tier(sticky_score),
                "reason": build_pinned_reason(candidate.reason, status),
            }
        )
    sticky_rows.sort(
        key=lambda row: (row["score"], row["confidence"], row["player_name"]),
        reverse=True,
    )
    return sticky_rows[:10]


def apply_hr_board_sliding_scale(*, base_score: float, previous_score: float | None, status: dict) -> float:
    phase = status.get("phase", "pregame")
    previous_score = float(previous_score or 0.0)
    minutes_to_start = status.get("minutes_to_start", 9999)
    inning = int(status.get("current_inning") or 0)
    probable_pitchers_confirmed = bool(status.get("probable_pitchers_confirmed"))
    is_lineup_window = bool(status.get("is_lineup_window"))

    if phase == "final":
        return 0.0

    if phase == "live":
        progress = min(max(inning, 1), 9) / 9.0
        live_factor = max(0.18, 1.0 - progress * 0.78)
        live_memory = previous_score * 0.18
        return round(base_score * live_factor + live_memory, 2)

    pregame_factor = 0.96
    if probable_pitchers_confirmed:
        pregame_factor += 0.04
    if is_lineup_window:
        pregame_factor += 0.08
    elif minutes_to_start > 240:
        pregame_factor -= 0.05

    sticky_memory = previous_score * 0.14
    return round(base_score * pregame_factor + sticky_memory, 2)


def build_pinned_reason(base_reason: str, status: dict) -> str:
    phase = status.get("phase", "pregame")
    if phase == "live":
        inning = status.get("current_inning") or "live"
        return f"{base_reason} | In game ({inning})"
    if phase == "final":
        return f"{base_reason} | Final"
    if status.get("is_lineup_window"):
        return f"{base_reason} | Near first pitch"
    return base_reason
