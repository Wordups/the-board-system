from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path

from app.builders.board_builder import sorted_candidates, to_player_row
from app.builders.diamond_board_builder import build_diamond, diamond_to_json
from app.builders.mlb_research_board import build_mlb_research_board
from app.builders.universal_game_builder import empty_markets
from app.collectors.mlb_collector import collect_mlb_raw_data
from app.models.mlb_model import normalize_mlb_inputs
from app.outputs.json_writer import write_json
from app.scoring.edge_score import score_candidate
from app.scoring.confidence import to_confidence
from app.scoring.tiers import assign_tier
from app.sim.edge import build_sim_board
from app.sim.sim_engine import sim_prob_pct, simulate_candidates
from app.utils.dates import timestamp_et


def is_public_hr_candidate(candidate) -> bool:
    return candidate.market != "HR" or candidate.tier in {"A", "B"}


def build_mlb_board(*, config, paths) -> dict:
    raw_payload = collect_mlb_raw_data(paths.data_raw)
    games_output = []
    pinned_candidates = []
    game_status_by_id = {}
    game_hr_results_by_id = {}

    processed_games = []
    for raw_game in raw_payload["games"]:
        candidates = [score_candidate(item) for item in normalize_mlb_inputs(raw_game)]
        simulate_candidates(candidates, sport="MLB")
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
                            "sim_prob_pct": sim_prob_pct(candidate),
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
            candidate for candidate in candidates if candidate.market == "HR" and is_public_hr_candidate(candidate)
        )
    previous_pinned_players = load_previous_pinned_players(paths)
    pinned_players = build_sticky_hr_board(
        candidates=sorted_candidates(pinned_candidates),
        previous_pinned_players=previous_pinned_players,
        game_status_by_id=game_status_by_id,
        game_hr_results_by_id=game_hr_results_by_id,
        play_of_day_name=load_hr_play_of_day_name(paths.data_raw / "mlb_research_notes.json"),
    )
    hr_core_players, hr_watch_players = split_hr_board_players(
        pinned_players,
        core_count=config.hr_core_count,
        watch_count=config.hr_watch_count,
    )
    consistency_players = build_consistency_board(processed_games)
    research_board = build_mlb_research_board(
        candidates=sorted_candidates(pinned_candidates),
        config=config,
        paths=paths,
    )
    hr_daily_picks = build_hr_daily_picks(research_board)
    sim_board = build_sim_board(
        [candidate for game in processed_games for candidate in game["candidates"]],
        sport="MLB",
    )
    return {
        "sport": "MLB",
        "date": raw_payload["date"],
        "last_updated": timestamp_et(),
        "pinned_board": {
            "title": "HR Core",
            "market": "HR",
            "players": hr_core_players,
        },
        "watch_board": {
            "title": "HR Watchlist",
            "market": "HR",
            "players": hr_watch_players,
        },
        "hr_board_meta": {
            "core_count": len(hr_core_players),
            "watch_count": len(hr_watch_players),
            "full_count": len(pinned_players),
        },
        "consistency_board": {
            "title": "Consistency Top 10",
            "market": "MIX",
            "players": consistency_players,
        },
        "daily_hr_picks": hr_daily_picks,
        "research_board": research_board,
        "sim_board": sim_board,
        "diamond": diamond_to_json(build_diamond(games_output, date=raw_payload["date"])),
        "games": games_output,
    }


def build_market_diverse_top_signals(*, candidates, limit: int) -> list[dict]:
    preferred_markets = ("HR", "RBI", "TB", "K", "Hits", "ML")
    selected = []
    used_markets = set()

    for market in preferred_markets:
        market_candidate = next(
            (
                candidate
                for candidate in candidates
                if candidate.tier != "PASS"
                and candidate.market == market
                and (market != "HR" or is_public_hr_candidate(candidate))
            ),
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
            if candidate.market == "HR" and not is_public_hr_candidate(candidate):
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
            "sim_prob_pct": sim_prob_pct(candidate),
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


def load_hr_play_of_day_name(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return str(payload.get("meta", {}).get("hr_play_of_day", "")).strip()


def build_sticky_hr_board(*, candidates, previous_pinned_players, game_status_by_id, game_hr_results_by_id, play_of_day_name: str = "") -> list[dict]:
    sticky_rows = []
    play_of_day_lookup = play_of_day_name.lower().strip()
    for candidate in candidates:
        status = game_status_by_id.get(candidate.game_id, {})
        hr_result = game_hr_results_by_id.get(candidate.game_id, {}).get(str(candidate.player_id), {})
        previous = previous_pinned_players.get(str(candidate.player_id))
        sticky_score = apply_hr_board_sliding_scale(
            base_score=candidate.score,
            previous_score=(previous or {}).get("score"),
            status=status,
        )
        if play_of_day_lookup and candidate.player_name.lower() == play_of_day_lookup:
            sticky_score = round(sticky_score + 3.5, 2)
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
                "reason": build_pinned_reason(candidate.reason, status, is_play_of_day=bool(play_of_day_lookup and candidate.player_name.lower() == play_of_day_lookup)),
                "sim_prob_pct": sim_prob_pct(candidate),
                "hr_result": hr_result.get("result", "pending"),
                "home_runs": int(hr_result.get("home_runs", 0)),
                "core_bucket": classify_hr_bucket(candidate, sticky_score),
                "projected_pa": round(float((candidate.extra or {}).get("projected_pa", 0.0) or 0.0), 2),
                "power_surge": round(float((candidate.extra or {}).get("power_surge", 0.0) or 0.0), 3),
                "hr_power_index": round(float((candidate.extra or {}).get("hr_power_index", 0.0) or 0.0), 3),
            }
        )
    sticky_rows.sort(
        key=lambda row: (row["score"], row["confidence"], row["player_name"]),
        reverse=True,
    )
    return sticky_rows[:10]


def classify_hr_bucket(candidate, sticky_score: float) -> str:
    extra = candidate.extra or {}
    projected_pa = float(extra.get("projected_pa", 0.0) or 0.0)
    power_surge = float(extra.get("power_surge", 0.0) or 0.0)
    hr_power_index = float(extra.get("hr_power_index", 0.0) or 0.0)
    pitcher_hr9 = float(extra.get("pitcher_hr9", 0.0) or 0.0)
    order_estimate = int(extra.get("order_estimate", 9) or 9)
    if (
        candidate.tier in {"A", "B"}
        and sticky_score >= 22
        and projected_pa >= 3.9
        and hr_power_index >= 0.52
        and order_estimate <= 5
        and (power_surge >= 0.38 or pitcher_hr9 >= 1.15)
    ):
        return "core"
    if sticky_score >= 18 and hr_power_index >= 0.4:
        return "strong"
    return "fringe"


def split_hr_board_players(players: list[dict], *, core_count: int, watch_count: int) -> tuple[list[dict], list[dict]]:
    core_players = [player for player in players if player.get("core_bucket") == "core"]
    if len(core_players) < core_count:
        for player in players:
            if player in core_players:
                continue
            core_players.append(player)
            if len(core_players) >= core_count:
                break

    core_lookup = {str(player.get("player_id")) for player in core_players}
    watch_players = [player for player in players if str(player.get("player_id")) not in core_lookup][:watch_count]
    return core_players[:core_count], watch_players


def build_consistency_board(processed_games) -> list[dict]:
    consistency_markets = {"Hits", "TB", "RBI", "K"}
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
                    "sim_prob_pct": sim_prob_pct(candidate),
                }
            )

    rows.sort(
        key=lambda row: (row["score"], row["confidence"], row["player_name"]),
        reverse=True,
    )
    return rows[:10]


def apply_consistency_bonus(candidate) -> float:
    market_bonus = {
        "Hits": 0.8,
        "TB": 2.4,
        "RBI": 2.1,
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


def build_pinned_reason(base_reason: str, status: dict, is_play_of_day: bool = False) -> str:
    if is_play_of_day:
        base_reason = f"{base_reason} | Bot POD"
    phase = status.get("phase", "pregame")
    if phase == "live":
        inning = status.get("current_inning") or "live"
        return f"{base_reason} | In game ({inning})"
    if phase == "final":
        return f"{base_reason} | Final"
    if status.get("is_lineup_window"):
        return f"{base_reason} | Near first pitch"
    return base_reason


def build_hr_daily_picks(research_board: dict) -> dict:
    hr_section = research_board.get("home_run", {}) if isinstance(research_board, dict) else {}
    top_candidates = hr_section.get("top_candidates", []) or []
    core_candidates = hr_section.get("core_candidates", []) or top_candidates
    parlays = hr_section.get("parlays", {}) or {}
    play_of_day = hr_section.get("play_of_day") or (core_candidates[0] if core_candidates else top_candidates[0] if top_candidates else None)

    return {
        "title": "HR Pick of the Day",
        "single": simplify_hr_pick(play_of_day),
        "two_leg": simplify_hr_parlay(parlays.get("2_leg", [])),
        "three_leg": simplify_hr_parlay(parlays.get("3_leg", [])),
    }


def simplify_hr_pick(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "player_id": str(row.get("player_id", "")),
        "player_name": row.get("player_name"),
        "team": row.get("team"),
        "opponent": row.get("opponent"),
        "line": row.get("line"),
        "score": round(float(row.get("score", 0.0)), 2),
        "tier": row.get("tier"),
        "reason": row.get("reason"),
        "play_of_day": bool(row.get("play_of_day")),
        "pitcher": row.get("pitcher"),
    }


def simplify_hr_parlay(rows: list[dict]) -> dict:
    legs = [
        {
            "player_name": row.get("player_name"),
            "team": row.get("team"),
            "opponent": row.get("opponent"),
            "line": row.get("line"),
            "score": round(float(row.get("score", 0.0)), 2),
            "tier": row.get("tier"),
            "reason": row.get("reason"),
        }
        for row in rows
    ]
    return {
        "legs": legs,
        "count": len(legs),
    }
