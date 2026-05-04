from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_nba_research_board(*, candidates: list[dict[str, Any]], config, paths) -> dict[str, Any]:
    notes = load_research_notes(paths.data_raw / "nba_research_notes.json")
    ranked = sorted(
        (apply_research_overlay(candidate, notes) for candidate in candidates if candidate.get("market") != "ML"),
        key=lambda item: item["score"],
        reverse=True,
    )

    safe_pool = [row for row in ranked if row["tier"] in {"A", "B"} and row["market"] in {"PTS", "AST", "REB"}]
    longshot_pool = [row for row in ranked if row["market"] in {"3PM", "PTS", "AST", "REB"}]

    return {
        "title": "NBA Research Board",
        "subtitle": "Safe stacks and long-shot stacks layered on top of the core NBA model.",
        "sources": [
            {"name": "ESPN schedule / gamelog", "type": "official"},
            {"name": "Opponent matchup and pace context", "type": "model"},
            {"name": "Head-to-head matchup history", "type": "model"},
            {"name": "Optional outside notes overlay", "type": "manual"},
        ],
        "external_notes_loaded": bool(notes.get("player_notes") or notes.get("game_notes")),
        "top_strip": ranked[:8],
        "safe_plays": {
            "title": "Safe Plays",
            "players": safe_pool[:12],
            "parlays": build_parlay_set(safe_pool, config, same_team_max=1, same_game_max=2),
        },
        "long_shots": {
            "title": "Long Shots",
            "players": longshot_pool[:12],
            "parlays": build_parlay_set(longshot_pool, config, same_team_max=2, same_game_max=2, longshot=True),
        },
        "sections": {
            "PTS": [row for row in ranked if row["market"] == "PTS"][:8],
            "AST": [row for row in ranked if row["market"] == "AST"][:8],
            "REB": [row for row in ranked if row["market"] == "REB"][:8],
            "3PM": [row for row in ranked if row["market"] == "3PM"][:8],
        },
    }


def load_research_notes(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"player_notes": {}, "game_notes": {}, "meta": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"player_notes": {}, "game_notes": {}, "meta": {"error": "Invalid JSON"}}
    return {
        "player_notes": payload.get("player_notes", {}),
        "game_notes": payload.get("game_notes", {}),
        "meta": payload.get("meta", {}),
    }


def apply_research_overlay(candidate: dict[str, Any], notes: dict[str, Any]) -> dict[str, Any]:
    player_notes = notes.get("player_notes", {}).get(candidate["player_name"], [])
    game_notes = notes.get("game_notes", {}).get(candidate["game_id"], [])
    evidence = list(player_notes) + list(game_notes)
    overlay_boost = sum(float(note.get("boost", 0.0)) for note in evidence if isinstance(note, dict))
    score = round(float(candidate["score"]) + overlay_boost, 2)
    row = {
        "player_id": str(candidate["player_id"]),
        "player_name": candidate["player_name"],
        "team": candidate["team"],
        "opponent": candidate["opponent"],
        "game_id": candidate["game_id"],
        "market": candidate["market"],
        "line": candidate["line"],
        "score": score,
        "base_score": round(float(candidate["score"]), 2),
        "confidence": int(candidate["confidence"]),
        "tier": candidate["tier"],
        "reason": candidate["reason"],
        "l10_hit_rate": candidate.get("l10_hit_rate"),
        "l5_hit_rate": candidate.get("l5_hit_rate"),
        "minutes_projection": candidate.get("minutes_projection"),
        "usage_rate": candidate.get("usage_rate"),
        "evidence": [
            note if isinstance(note, dict) else {"source": "manual", "note": str(note), "boost": 0}
            for note in evidence[:6]
        ],
    }
    return row


def build_parlay_set(
    pool: list[dict[str, Any]],
    config,
    *,
    same_team_max: int,
    same_game_max: int,
    longshot: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    return {
        f"{leg_size}_leg": build_parlay_legs(
            pool,
            leg_size,
            same_team_max=same_team_max,
            same_game_max=same_game_max,
            longshot=longshot,
        )
        for leg_size in config.parlay_leg_sizes if leg_size <= 4
    }


def build_parlay_legs(
    pool: list[dict[str, Any]],
    leg_size: int,
    *,
    same_team_max: int,
    same_game_max: int,
    longshot: bool,
) -> list[dict[str, Any]]:
    team_counts: dict[str, int] = {}
    game_counts: dict[str, int] = {}
    selected: list[dict[str, Any]] = []
    ranked = sorted(
        pool,
        key=lambda row: (
            row["score"],
            1 if row["market"] == "3PM" and longshot else 0,
            row["player_name"],
        ),
        reverse=True,
    )

    for row in ranked:
        if team_counts.get(row["team"], 0) >= same_team_max:
            continue
        if game_counts.get(row["game_id"], 0) >= same_game_max:
            continue
        if not longshot and row["market"] == "3PM" and row["tier"] not in {"A", "B"}:
            continue
        selected.append(
            {
                "player_name": row["player_name"],
                "team": row["team"],
                "market": row["market"],
                "line": row["line"],
                "score": row["score"],
                "tier": row["tier"],
                "reason": row["reason"],
                "evidence": row.get("evidence", [])[:2],
            }
        )
        team_counts[row["team"]] = team_counts.get(row["team"], 0) + 1
        game_counts[row["game_id"]] = game_counts.get(row["game_id"], 0) + 1
        if len(selected) >= leg_size:
            break

    return selected
