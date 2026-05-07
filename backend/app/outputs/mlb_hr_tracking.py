from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.outputs.json_writer import write_json


ET = ZoneInfo("America/New_York")


def write_mlb_hr_tracking_snapshot(*, board: dict[str, Any], paths) -> None:
    if not board or board.get("sport") != "MLB":
        return

    payload = build_mlb_hr_tracking_payload(board=board)
    latest_path = paths.data_final / "mlb_hr_tracking_latest.json"
    history_dir = paths.data_final / "history" / "mlb_hr_tracking"
    history_path = history_dir / f"{payload['date']}.json"

    write_json(latest_path, payload)
    if not history_path.exists():
        write_json(history_path, payload)


def build_mlb_hr_tracking_payload(*, board: dict[str, Any]) -> dict[str, Any]:
    board_date = str(board.get("date") or "")
    last_updated = str(board.get("last_updated") or "")
    generated_at = format_et_timestamp(datetime.now(ET))
    pinned_rows = board.get("pinned_board", {}).get("players", []) or []
    research_section = ((board.get("research_board") or {}).get("home_run") or {})
    top_candidates = research_section.get("top_candidates", []) or []
    daily_hr_picks = board.get("daily_hr_picks", {}) or {}

    pinned_by_name = {
        normalize_name(row.get("player_name")): {**row, "_rank": index}
        for index, row in enumerate(pinned_rows, start=1)
    }
    tracked_rows = []
    for index, candidate in enumerate(top_candidates, start=1):
        pinned = pinned_by_name.get(normalize_name(candidate.get("player_name")), {})
        tracked_rows.append(
            build_tracking_row(
                candidate=candidate,
                pinned_row=pinned,
                research_rank=index,
                tags=build_membership_tags(candidate=candidate, daily_hr_picks=daily_hr_picks),
            )
        )

    return {
        "sport": "MLB",
        "date": board_date,
        "generated_at": generated_at,
        "board_last_updated": last_updated,
        "tracking_version": 1,
        "summary": {
            "pinned_board_title": board.get("pinned_board", {}).get("title", "HR Top 10"),
            "tracked_candidate_count": len(tracked_rows),
            "core_candidate_count": sum(1 for row in tracked_rows if row.get("core_fringe_tag") == "core"),
            "fringe_candidate_count": sum(1 for row in tracked_rows if row.get("core_fringe_tag") == "fringe"),
        },
        "daily_picks": build_daily_pick_summary(daily_hr_picks),
        "tracked_candidates": tracked_rows,
    }


def build_tracking_row(
    *,
    candidate: dict[str, Any],
    pinned_row: dict[str, Any],
    research_rank: int,
    tags: dict[str, bool],
) -> dict[str, Any]:
    player_id = str(candidate.get("player_id") or "")
    player_name = str(candidate.get("player_name") or "")
    score = safe_float(candidate.get("score"))
    base_score = safe_float(candidate.get("base_score"))

    return {
        "player_id": player_id,
        "player_name": player_name,
        "team": candidate.get("team"),
        "opponent": candidate.get("opponent"),
        "game_id": candidate.get("game_id"),
        "market": candidate.get("market", "HR"),
        "line": candidate.get("line"),
        "score": score,
        "base_score": base_score,
        "confidence": candidate.get("confidence"),
        "tier": candidate.get("tier"),
        "reason": candidate.get("reason", ""),
        "research_rank": research_rank,
        "pinned_rank": find_pinned_rank(pinned_row),
        "core_fringe_tag": classify_core_fringe(
            research_rank=research_rank,
            tags=tags,
            candidate_bucket=str(candidate.get("hr_bucket") or ""),
        ),
        "board_tags": {
            "play_of_day": bool(candidate.get("play_of_day")),
            "daily_straight": tags["daily_straight"],
            "daily_two_leg": tags["daily_two_leg"],
            "daily_three_leg": tags["daily_three_leg"],
            "pinned_board": bool(pinned_row),
        },
        "result": {
            "status": pinned_row.get("hr_result", "pending") if pinned_row else "pending",
            "home_runs": int(pinned_row.get("home_runs", 0) or 0),
        },
        "features": {
            "pitcher_name": candidate.get("pitcher"),
            "pitcher_whip": safe_float(candidate.get("whip")),
            "pitcher_hr9": safe_float(candidate.get("hr9")),
            "pitcher_hr_allowed": safe_float(candidate.get("hr_allowed")),
            "vs_pitcher_avg": safe_float(candidate.get("vs_pitcher_avg")),
            "vs_pitcher_ops": safe_float(candidate.get("vs_pitcher_ops")),
            "vs_pitcher_hr": safe_float(candidate.get("vs_pitcher_hr")),
            "order_estimate": safe_int(candidate.get("order_estimate")),
            "projected_pa": safe_float(candidate.get("projected_pa")),
            "lineup_confirmed": derive_lineup_confirmed(candidate),
            "lineup_uncertainty_penalty": derive_lineup_uncertainty_penalty(candidate),
            "season_hr_probability": extract_feature_float(candidate, "season_hr_probability"),
            "l10_hr_probability": extract_feature_float(candidate, "l10_hr_probability"),
            "l5_hr_probability": extract_feature_float(candidate, "l5_hr_probability"),
            "historical_hr_probability": extract_feature_float(candidate, "historical_hr_probability"),
            "season_hr_per_game": extract_feature_float(candidate, "season_hr_per_game"),
            "l10_hr_per_game": extract_feature_float(candidate, "l10_hr_per_game"),
            "l5_hr_per_game": extract_feature_float(candidate, "l5_hr_per_game"),
            "ops": extract_feature_float(candidate, "ops"),
            "slg": extract_feature_float(candidate, "slg"),
            "iso": extract_feature_float(candidate, "iso"),
            "sample_reliability": extract_feature_float(candidate, "sample_reliability"),
            "age": extract_feature_float(candidate, "age"),
            "historical_power_index": extract_feature_float(candidate, "historical_power_index"),
            "recent_peak_hr_rate": extract_feature_float(candidate, "recent_peak_hr_rate"),
            "career_hr_rate": extract_feature_float(candidate, "career_hr_rate"),
        },
        "evidence": normalize_evidence(candidate.get("evidence", [])),
    }


def build_daily_pick_summary(daily_hr_picks: dict[str, Any]) -> dict[str, Any]:
    return {
        "single": summarize_pick_row(daily_hr_picks.get("single")),
        "two_leg": [summarize_pick_row(leg) for leg in ((daily_hr_picks.get("two_leg") or {}).get("legs") or []) if leg],
        "three_leg": [summarize_pick_row(leg) for leg in ((daily_hr_picks.get("three_leg") or {}).get("legs") or []) if leg],
    }


def summarize_pick_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "player_name": row.get("player_name"),
        "team": row.get("team"),
        "opponent": row.get("opponent"),
        "line": row.get("line"),
        "score": safe_float(row.get("score")),
        "tier": row.get("tier"),
    }


def build_membership_tags(*, candidate: dict[str, Any], daily_hr_picks: dict[str, Any]) -> dict[str, bool]:
    player_id = str(candidate.get("player_id") or "")
    player_name = normalize_name(candidate.get("player_name"))

    def row_matches(row: dict[str, Any] | None) -> bool:
        if not row:
            return False
        row_id = str(row.get("player_id") or "")
        row_name = normalize_name(row.get("player_name"))
        return (player_id and row_id == player_id) or (player_name and row_name == player_name)

    two_leg_rows = ((daily_hr_picks.get("two_leg") or {}).get("legs") or [])
    three_leg_rows = ((daily_hr_picks.get("three_leg") or {}).get("legs") or [])
    return {
        "daily_straight": row_matches(daily_hr_picks.get("single")),
        "daily_two_leg": any(row_matches(row) for row in two_leg_rows),
        "daily_three_leg": any(row_matches(row) for row in three_leg_rows),
    }


def classify_core_fringe(*, research_rank: int, tags: dict[str, bool], candidate_bucket: str) -> str:
    if candidate_bucket in {"core", "strong", "fringe"}:
        return candidate_bucket
    if tags["daily_straight"] or research_rank <= 3:
        return "core"
    return "fringe"


def find_pinned_rank(pinned_row: dict[str, Any]) -> int | None:
    if not pinned_row:
        return None
    rank = pinned_row.get("_rank", pinned_row.get("rank"))
    if isinstance(rank, int):
        return rank
    return None


def derive_lineup_confirmed(candidate: dict[str, Any]) -> bool | None:
    feature = extract_feature(candidate, "lineup_confirmed")
    if feature is None:
        return None
    return bool(feature)


def derive_lineup_uncertainty_penalty(candidate: dict[str, Any]) -> float | None:
    return extract_feature_float(candidate, "lineup_uncertainty_penalty")


def extract_feature(candidate: dict[str, Any], key: str) -> Any:
    evidence = candidate.get("evidence", [])
    for item in evidence:
        if isinstance(item, dict) and item.get("key") == key:
            return item.get("value")
    return candidate.get(key)


def extract_feature_float(candidate: dict[str, Any], key: str) -> float | None:
    value = extract_feature(candidate, key)
    return safe_float(value)


def normalize_evidence(evidence: list[Any]) -> list[dict[str, Any]]:
    normalized = []
    for item in evidence:
        if isinstance(item, dict):
            normalized.append(item)
        else:
            normalized.append({"note": str(item)})
    return normalized


def normalize_name(value: Any) -> str:
    return str(value or "").strip().lower()


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_et_timestamp(dt: datetime) -> str:
    hour = dt.strftime("%I").lstrip("0") or "0"
    return f"{dt.date().isoformat()} {hour}:{dt.strftime('%M %p')} ET"
