from __future__ import annotations

from collections import defaultdict
import json

from app.builders.board_builder import sorted_candidates, to_player_row
from app.builders.mlb_research_board import build_mlb_research_board
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
    game_hr_results_by_id = {}

    processed_games = []
    for raw_game in raw_payload["games"]:
        candidates = [score_candidate(item) for item in normalize_mlb_inputs(raw_game)]
        processed_games.append({"raw": raw_game, "candidates": candidates})
        game_status_by_id[raw_game["game_id"]] = raw_game.get("status", {})
        game_hr_results_by_id[raw_game["game_id"]] = raw_game.get("player_hr_results", {})

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

        top_signals = build_market_diverse_top_signals(
            candidates=candidates,
            limit=config.top_signals_per_game,
        )

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
        game_hr_results_by_id=game_hr_results_by_id,
    )
    consistency_players = build_consistency_board(processed_games)
    return {
        "sport": "MLB",
        "date": raw_payload["date"],
        "last_updated": timestamp_et(),
        "pinned_board": {
            "title": "HR Top 10",
            "market": "HR",
            "players": pinned_players,
        },
        "consistency_board": {
            "title": "Consistency Top 10",
            "market": "MIX",
            "players": consistency_players,
        },
        "research_board": build_mlb_research_board(
            candidates=sorted_candidates(pinned_candidates),
            config=config,
            paths=paths,
        ),
        "games": games_output,
    }


def build_market_diverse_top_signals(*, candidates, limit: int) -> list[dict]:
    preferred_markets = ("Hits", "TB", "K", "HR", "ML")
    selected = []
    used_markets = set()

    for market in preferred_markets:
        market_candidate = next(
            (candidate for candidate in candidates if candidate.tier != "PASS" and candidate.market == market),
            None,
        )
        if market_candidate is None:
            continue
        selected.append(market_candidate)
        used_markets.add((market_candidate.market, market_candidate.player_id))
        if len(selected) == limit:
            break

    if len(selected) < limit:
        for candidate in candidates:
            key = (candidate.market, candidate.player_id)
            if candidate.tier == "PASS" or key in used_markets:
                continue
            selected.append(candidate)
            used_markets.add(key)
            if len(selected) == limit:
                break

    return [
        {
            "market": candidate.market,
            "player_name": candidate.player_name,
            "line": candidate.line,
            "score": candidate.score,
            "confidence": candidate.confidence,
            "tier": candidate.tier,
        }
        for candidate in selected
    ]


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


def build_sticky_hr_board(*, candidates, previous_pinned_players, game_status_by_id, game_hr_results_by_id) -> list[dict]:
    sticky_rows = []
    for candidate in candidates:
        status = game_status_by_id.get(candidate.game_id, {})
        hr_result = game_hr_results_by_id.get(candidate.game_id, {}).get(str(candidate.player_id), {})
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
                "hr_result": hr_result.get("result", "pending"),
                "home_runs": int(hr_result.get("home_runs", 0)),
            }
        )
    sticky_rows.sort(
        key=lambda row: (row["score"], row["confidence"], row["player_name"]),
        reverse=True,
    )
    return sticky_rows[:10]


def build_consistency_board(processed_games) -> list[dict]:
    consistency_markets = {"Hits", "TB", "K"}
    rows = []
    for processed_game in processed_games:
        for candidate in sorted_candidates(processed_game["candidates"]):
            if candidate.market not in consistency_markets or candidate.tier == "PASS":
                continue
            rows.append(
                {
                    "player_id": candidate.player_id,
                    "player_name": candidate.player_name,
                    "team": candidate.team,
                    "opponent": candidate.opponent,
                    "line": candidate.line,
                    "score": round(apply_consistency_bonus(candidate), 2),
                    "confidence": candidate.confidence,
                    "tier": candidate.tier,
                    "reason": candidate.reason,
                }
            )

    rows.sort(
        key=lambda row: (row["score"], row["confidence"], row["player_name"]),
        reverse=True,
    )
    return rows[:10]


def apply_consistency_bonus(candidate) -> float:
    market_bonus = {
        "Hits": 3.2,
        "TB": 1.8,
        "K": 2.4,
    }.get(candidate.market, 0.0)
    tier_bonus = {
        "A": 1.0,
        "B": 0.4,
        "C": 0.0,
    }.get(candidate.tier, 0.0)
    return candidate.score + market_bonus + tier_bonus


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
        live_factor = max(0.0, 1.0 - progress * 0.92)
        if inning >= 8:
            live_factor *= 0.35
        elif inning >= 6:
            live_factor *= 0.6
        live_memory = previous_score * (0.12 if inning < 6 else 0.05)
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
