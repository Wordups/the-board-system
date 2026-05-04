from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models.mlb_model import MlbPlayCandidate


def build_mlb_research_board(*, candidates: list[MlbPlayCandidate], config, paths) -> dict[str, Any]:
    notes = load_research_notes(paths.data_raw / "mlb_research_notes.json")
    grouped = {
        "HR": [c for c in candidates if c.market == "HR" and c.tier != "PASS"],
        "Hits": [c for c in candidates if c.market == "Hits" and c.tier != "PASS"],
        "TB": [c for c in candidates if c.market == "TB" and c.tier != "PASS"],
        "K": [c for c in candidates if c.market == "K" and c.tier != "PASS"],
    }

    return {
        "title": "Research Board",
        "subtitle": "Daily parlay layer built from model signals plus optional outside research notes.",
        "glossary": {
            "WHIP": "Walks plus hits allowed per inning pitched.",
        },
        "sources": [
            {"name": "MLB Stats API", "type": "official"},
            {"name": "Baseball Savant / Statcast", "type": "official"},
            {"name": "Probable Pitchers", "type": "official"},
            {"name": "Weather / park context", "type": "environment"},
            {"name": "Optional research notes overlay", "type": "manual"},
        ],
        "external_notes_loaded": bool(notes.get("player_notes") or notes.get("game_notes") or notes.get("pitcher_notes")),
        "home_run": build_market_research_section(
            market_name="HR",
            label="Home Run Board",
            candidates=grouped["HR"],
            config=config,
            notes=notes,
            hr_of_day_count=3,
        ),
        "hits": build_market_research_section(
            market_name="Hits",
            label="Hits Board",
            candidates=grouped["Hits"],
            config=config,
            notes=notes,
        ),
        "total_bases": build_market_research_section(
            market_name="TB",
            label="Total Bases Board",
            candidates=grouped["TB"],
            config=config,
            notes=notes,
        ),
        "strikeouts": build_market_research_section(
            market_name="K",
            label="Strikeouts Board",
            candidates=grouped["K"],
            config=config,
            notes=notes,
        ),
    }


def load_research_notes(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"player_notes": {}, "game_notes": {}, "pitcher_notes": {}, "meta": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"player_notes": {}, "game_notes": {}, "pitcher_notes": {}, "meta": {"error": "Invalid JSON"}}
    return {
        "player_notes": payload.get("player_notes", {}),
        "game_notes": payload.get("game_notes", {}),
        "pitcher_notes": payload.get("pitcher_notes", {}),
        "meta": payload.get("meta", {}),
    }


def build_market_research_section(
    *,
    market_name: str,
    label: str,
    candidates: list[MlbPlayCandidate],
    config,
    notes: dict[str, Any],
    hr_of_day_count: int = 0,
) -> dict[str, Any]:
    ranked = sorted(
        (apply_research_overlay(candidate, notes) for candidate in candidates),
        key=lambda item: item["score"],
        reverse=True,
    )
    parlays = {
        f"{leg_size}_leg": build_parlay_legs(ranked, leg_size, config)
        for leg_size in config.parlay_leg_sizes
    }
    response = {
        "title": label,
        "top_candidates": ranked[:12],
        "parlays": parlays,
    }
    if hr_of_day_count:
        response["hr_of_day"] = ranked[:hr_of_day_count]
    return response


def apply_research_overlay(candidate: MlbPlayCandidate, notes: dict[str, Any]) -> dict[str, Any]:
    extra = candidate.extra or {}
    player_notes = notes.get("player_notes", {}).get(candidate.player_name, [])
    pitcher_notes = notes.get("pitcher_notes", {}).get(str(extra.get("pitcher_name", "")), [])
    game_notes = notes.get("game_notes", {}).get(candidate.game_id, [])
    evidence = list(player_notes) + list(pitcher_notes) + list(game_notes)
    overlay_boost = sum(float(note.get("boost", 0.0)) for note in evidence if isinstance(note, dict))
    adjusted_score = round(candidate.score + overlay_boost, 2)

    return {
        "player_id": candidate.player_id,
        "player_name": candidate.player_name,
        "team": candidate.team,
        "opponent": candidate.opponent,
        "game_id": candidate.game_id,
        "market": candidate.market,
        "line": candidate.line,
        "score": adjusted_score,
        "base_score": round(candidate.score, 2),
        "confidence": candidate.confidence,
        "tier": candidate.tier,
        "reason": candidate.reason,
        "pitcher": extra.get("pitcher_name"),
        "whip": extra.get("pitcher_whip"),
        "hr9": extra.get("pitcher_hr9"),
        "hr_allowed": extra.get("pitcher_hr_allowed"),
        "vs_pitcher_avg": extra.get("vs_pitcher_avg"),
        "vs_pitcher_ops": extra.get("vs_pitcher_ops"),
        "vs_pitcher_hr": extra.get("vs_pitcher_hr"),
        "order_estimate": extra.get("order_estimate"),
        "projected_pa": extra.get("projected_pa"),
        "evidence": [
            note if isinstance(note, dict) else {"source": "manual", "note": str(note), "boost": 0}
            for note in evidence[:6]
        ],
    }


def build_parlay_legs(ranked: list[dict[str, Any]], leg_size: int, config) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    team_counts: dict[str, int] = {}
    game_counts: dict[str, int] = {}

    for row in ranked:
        if team_counts.get(row["team"], 0) >= config.parlay_max_same_team:
            continue
        if game_counts.get(row["game_id"], 0) >= config.parlay_max_same_game:
            continue
        selected.append(
            {
                "player_name": row["player_name"],
                "team": row["team"],
                "opponent": row["opponent"],
                "line": row["line"],
                "score": row["score"],
                "tier": row["tier"],
                "reason": row["reason"],
                "pitcher": row.get("pitcher"),
                "evidence": row.get("evidence", [])[:2],
            }
        )
        team_counts[row["team"]] = team_counts.get(row["team"], 0) + 1
        game_counts[row["game_id"]] = game_counts.get(row["game_id"], 0) + 1
        if len(selected) >= leg_size:
            break

    return selected
