from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models.mlb_model import MlbPlayCandidate


def build_mlb_research_board(*, candidates: list[MlbPlayCandidate], config, paths) -> dict[str, Any]:
    notes = load_research_notes(paths.data_raw / "mlb_research_notes.json")
    grouped = {
        "HR": [c for c in candidates if c.market == "HR" and c.tier in {"A", "B"}],
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
    explicit_play = str(notes.get("meta", {}).get("hr_play_of_day", "")).strip().lower()
    core_candidates = [row for row in ranked if row.get("hr_bucket") == "core"]
    strong_candidates = [row for row in ranked if row.get("hr_bucket") == "strong"]
    fringe_candidates = [row for row in ranked if row.get("hr_bucket") == "fringe"]
    parlays = {
        f"{leg_size}_leg": build_parlay_legs(
            ranked,
            leg_size,
            config,
            market_name=market_name,
        )
        for leg_size in config.parlay_leg_sizes
    }
    response = {
        "title": label,
        "top_candidates": ranked[:12],
        "parlays": parlays,
    }
    if hr_of_day_count:
        response["core_candidates"] = core_candidates[: config.hr_core_count]
        response["strong_candidates"] = strong_candidates[: max(config.hr_watch_count // 2, 3)]
        response["fringe_candidates"] = fringe_candidates[: config.hr_watch_count]
        response["hr_of_day"] = ranked[:hr_of_day_count]
        response["play_of_day"] = next(
            (row for row in ranked if explicit_play and row["player_name"].lower() == explicit_play),
            core_candidates[0] if core_candidates else ranked[0] if ranked else None,
        )
    return response


def apply_research_overlay(candidate: MlbPlayCandidate, notes: dict[str, Any]) -> dict[str, Any]:
    extra = candidate.extra or {}
    player_notes = notes.get("player_notes", {}).get(candidate.player_name, [])
    pitcher_notes = notes.get("pitcher_notes", {}).get(str(extra.get("pitcher_name", "")), [])
    game_notes = notes.get("game_notes", {}).get(candidate.game_id, [])
    evidence = list(player_notes) + list(pitcher_notes) + list(game_notes)
    overlay_boost = sum(float(note.get("boost", 0.0)) for note in evidence if isinstance(note, dict))
    explicit_play = str(notes.get("meta", {}).get("hr_play_of_day", "")).strip().lower()
    play_of_day_boost = 3.5 if candidate.market == "HR" and explicit_play and candidate.player_name.lower() == explicit_play else 0.0
    adjusted_score = round(candidate.score + overlay_boost, 2)
    adjusted_score = round(adjusted_score + play_of_day_boost, 2)
    hr_bucket = classify_hr_candidate(candidate, adjusted_score)

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
        "play_of_day": play_of_day_boost > 0.0,
        "hr_bucket": hr_bucket,
        "pitcher": extra.get("pitcher_name"),
        "whip": extra.get("pitcher_whip"),
        "hr9": extra.get("pitcher_hr9"),
        "hr_allowed": extra.get("pitcher_hr_allowed"),
        "vs_pitcher_avg": extra.get("vs_pitcher_avg"),
        "vs_pitcher_ops": extra.get("vs_pitcher_ops"),
        "vs_pitcher_hr": extra.get("vs_pitcher_hr"),
        "order_estimate": extra.get("order_estimate"),
        "projected_pa": extra.get("projected_pa"),
        "lineup_confirmed": extra.get("lineup_confirmed"),
        "lineup_uncertainty_penalty": extra.get("lineup_uncertainty_penalty"),
        "season_hr_probability": extra.get("season_hr_probability"),
        "l10_hr_probability": extra.get("l10_hr_probability"),
        "l5_hr_probability": extra.get("l5_hr_probability"),
        "historical_hr_probability": extra.get("historical_hr_probability"),
        "season_hr_per_game": extra.get("season_hr_per_game"),
        "l10_hr_per_game": extra.get("l10_hr_per_game"),
        "l5_hr_per_game": extra.get("l5_hr_per_game"),
        "ops": extra.get("ops"),
        "slg": extra.get("slg"),
        "iso": extra.get("iso"),
        "sample_reliability": extra.get("sample_reliability"),
        "age": extra.get("age"),
        "historical_power_index": extra.get("historical_power_index"),
        "recent_peak_hr_rate": extra.get("recent_peak_hr_rate"),
        "career_hr_rate": extra.get("career_hr_rate"),
        "evidence": [
            note if isinstance(note, dict) else {"source": "manual", "note": str(note), "boost": 0}
            for note in evidence[:6]
        ],
    }


def classify_hr_candidate(candidate: MlbPlayCandidate, score: float) -> str:
    if candidate.market != "HR":
        return "support"
    extra = candidate.extra or {}
    projected_pa = float(extra.get("projected_pa", 0.0) or 0.0)
    order_estimate = int(extra.get("order_estimate", 9) or 9)
    hr_power_index = float(extra.get("hr_power_index", 0.0) or 0.0)
    power_surge = float(extra.get("power_surge", 0.0) or 0.0)
    pitcher_hr9 = float(extra.get("pitcher_hr9", 0.0) or 0.0)
    if (
        candidate.tier in {"A", "B"}
        and score >= 22
        and projected_pa >= 3.9
        and hr_power_index >= 0.52
        and order_estimate <= 5
        and (power_surge >= 0.38 or pitcher_hr9 >= 1.15)
    ):
        return "core"
    if score >= 18 and hr_power_index >= 0.4:
        return "strong"
    return "fringe"


def build_parlay_legs(ranked: list[dict[str, Any]], leg_size: int, config, *, market_name: str) -> list[dict[str, Any]]:
    if market_name == "HR":
        return build_hr_parlay_legs(ranked, leg_size, config)
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


def build_hr_parlay_legs(ranked: list[dict[str, Any]], leg_size: int, config) -> list[dict[str, Any]]:
    bucket_priority = ("core", "strong", "fringe")
    selected: list[dict[str, Any]] = []
    team_counts: dict[str, int] = {}
    game_counts: dict[str, int] = {}
    used_players: set[str] = set()

    for bucket in bucket_priority:
        bucket_rows = [row for row in ranked if row.get("hr_bucket") == bucket]
        for row in bucket_rows:
            if len(selected) >= leg_size:
                break
            player_key = str(row.get("player_id", ""))
            if player_key in used_players:
                continue
            if team_counts.get(row["team"], 0) >= config.parlay_max_same_team:
                continue
            if game_counts.get(row["game_id"], 0) >= config.parlay_max_same_game:
                continue
            selected.append(compact_parlay_leg(row))
            used_players.add(player_key)
            team_counts[row["team"]] = team_counts.get(row["team"], 0) + 1
            game_counts[row["game_id"]] = game_counts.get(row["game_id"], 0) + 1
        if len(selected) >= leg_size:
            break

    if len(selected) < leg_size:
        for row in ranked:
            if len(selected) >= leg_size:
                break
            player_key = str(row.get("player_id", ""))
            if player_key in used_players:
                continue
            if team_counts.get(row["team"], 0) >= config.parlay_max_same_team:
                continue
            if game_counts.get(row["game_id"], 0) >= config.parlay_max_same_game:
                continue
            selected.append(compact_parlay_leg(row))
            used_players.add(player_key)
            team_counts[row["team"]] = team_counts.get(row["team"], 0) + 1
            game_counts[row["game_id"]] = game_counts.get(row["game_id"], 0) + 1

    return selected


def compact_parlay_leg(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "player_name": row["player_name"],
        "team": row["team"],
        "opponent": row["opponent"],
        "line": row["line"],
        "score": row["score"],
        "tier": row["tier"],
        "reason": row["reason"],
        "pitcher": row.get("pitcher"),
        "evidence": row.get("evidence", [])[:2],
        "hr_bucket": row.get("hr_bucket"),
    }
