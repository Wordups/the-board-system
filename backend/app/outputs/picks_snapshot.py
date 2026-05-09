from __future__ import annotations

import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

from app.outputs.json_writer import write_json


ET = ZoneInfo("America/New_York")


def local_iso_date() -> str:
    return datetime.now(ET).date().isoformat()


def format_et_timestamp(dt: datetime) -> str:
    hour = dt.strftime("%I").lstrip("0") or "0"
    return f"{hour}:{dt.strftime('%M %p')} ET"


def line_floor_value(line: str) -> float:
    if not line:
        return 0.0
    token = str(line).split()[0].replace("+", "").replace("-", "")
    try:
        return float(token)
    except ValueError:
        return 0.0


def desired_line_floor(sport: str, market: str) -> float:
    floors = {
        "mlb": {"HR": 1, "Hits": 2, "TB": 2, "RBI": 2, "K": 6},
        "nba": {"PTS": 15, "AST": 5, "REB": 6, "3PM": 2},
        "wnba": {"PTS": 12, "AST": 4, "REB": 5, "3PM": 2},
        "soccer": {"GS": 1},
        "tennis": {"ML": 0, "Sets": 2, "O/U": 20.5},
    }
    return floors.get(sport, {}).get(market, 0.0)


def prop_quality_score(row: dict, sport: str) -> float:
    market = str(row.get("market", ""))
    tier = str(row.get("tier", "C"))
    tier_bonus = {"A": 5, "B": 2, "C": -2}.get(tier, -6)
    market_bonus_map = {
        "mlb": {"HR": 4, "Hits": 3, "TB": 3, "RBI": 2, "K": 1},
        "nba": {"PTS": 2, "AST": 3, "REB": 2.5, "3PM": 1},
        "wnba": {"PTS": 2, "AST": 3, "REB": 2.5, "3PM": 1},
        "soccer": {"GS": 3, "AST": 2, "OU": 0, "ML": 0},
        "tennis": {"ML": 3, "Sets": 2, "O/U": 0.5},
    }
    market_bonus = market_bonus_map.get(sport, {}).get(market, 0.0)
    floor = desired_line_floor(sport, market)
    line_value = line_floor_value(str(row.get("line", "")))
    line_bonus = (line_value - floor) * 0.6 if line_value >= floor else -10 - (floor - line_value) * 3
    reason = str(row.get("reason", ""))
    form_bonus = 0.0
    if "L5 80" in reason or "L5 90" in reason or "L5 100" in reason:
        form_bonus += 2.0
    if "H2H" in reason:
        form_bonus += 1.2
    if "WHIP" in reason:
        form_bonus += 0.8
    if "Platoon +" in reason:
        form_bonus += 0.8
    if "Rising" in reason or "Uptick" in reason:
        form_bonus += 0.8
    if "Decline" in reason:
        form_bonus -= 1.4
    return round(float(row.get("score", 0.0)) + tier_bonus + market_bonus + line_bonus + form_bonus, 2)


def clone_row(row: dict | None, market_override: str = "") -> dict | None:
    if not row:
        return None
    cloned = {
        "player_id": row.get("player_id"),
        "player_name": row.get("player_name"),
        "team": row.get("team"),
        "opponent": row.get("opponent"),
        "game_id": row.get("game_id"),
        "line": row.get("line"),
        "score": row.get("score"),
        "tier": row.get("tier") or "C",
        "reason": row.get("reason", ""),
        "market": market_override or row.get("market") or "",
    }
    for key in (
        "implied_odds",
        "value_zone",
        "edge",
        "model_hit_rate",
        "lineup_status",
        "team_star_outs",
        "team_star_gtd",
        "team_usage_boost",
        "team_lost_usage",
    ):
        if key in row:
            cloned[key] = row[key]
    return cloned


def dedupe_rows(rows: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for row in rows:
        key = f'{row.get("player_id") or row.get("player_name")}:{row.get("market")}:{row.get("line")}'
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def select_daily_rows(rows: list[dict], sport: str, count: int, *, same_game_max: int, same_team_max: int) -> list[dict]:
    ranked = sorted(
        [row for row in dedupe_rows(rows) if prop_quality_score(row, sport) > 0],
        key=lambda row: prop_quality_score(row, sport),
        reverse=True,
    )
    selected: list[dict] = []
    game_counts: dict[str, int] = {}
    team_counts: dict[str, int] = {}
    for row in ranked:
        game_id = str(row.get("game_id") or f'{row.get("team")}-{row.get("opponent")}')
        team = str(row.get("team") or row.get("player_name"))
        if game_counts.get(game_id, 0) >= same_game_max:
            continue
        if team_counts.get(team, 0) >= same_team_max:
            continue
        selected.append(row)
        game_counts[game_id] = game_counts.get(game_id, 0) + 1
        team_counts[team] = team_counts.get(team, 0) + 1
        if len(selected) >= count:
            break
    return selected


def _is_a_tier(row: dict | None) -> bool:
    return bool(row) and str(row.get("tier", "")).upper() == "A"


def derive_sport_picks(sport: str, data: dict) -> dict | None:
    """Derive frozen daily picks for the morning card.

    Strict A-tier policy: only tier=='A' rows can land on the picks of the
    day. If a sport has no A-tier straight, that sport is dropped from the
    snapshot entirely. Parlay slots accept only A-tier rows; if there
    aren't enough, the slot stays short rather than fall back to B/C.
    """
    if not data or data.get("date") != local_iso_date():
        return None

    if sport == "mlb":
        hr = data.get("daily_hr_picks", {})
        straight = clone_row(hr.get("single"), "HR")
        two_leg = [clone_row(row, "HR") for row in (hr.get("two_leg", {}).get("legs") or []) if row]
        three_leg = [clone_row(row, "HR") for row in (hr.get("three_leg", {}).get("legs") or []) if row]
        if not _is_a_tier(straight):
            return None
        return {
            "straight": straight,
            "twoLeg": [row for row in two_leg if _is_a_tier(row)],
            "threeLeg": [row for row in three_leg if _is_a_tier(row)],
        }

    if sport == "nba":
        pool = [
            *[clone_row(row, row.get("market", "")) for row in data.get("research_board", {}).get("safe_plays", {}).get("players", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("research_board", {}).get("sections", {}).get("PTS", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("research_board", {}).get("sections", {}).get("AST", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("research_board", {}).get("sections", {}).get("REB", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("research_board", {}).get("sections", {}).get("3PM", [])],
        ]
        pool = [row for row in pool if _is_a_tier(row)]
        if not pool:
            return None
        straight = select_daily_rows(pool, "nba", 1, same_game_max=1, same_team_max=1)
        parlay_pool = select_daily_rows(pool, "nba", 8, same_game_max=2, same_team_max=1)
        return {
            "straight": straight[0] if straight else None,
            "twoLeg": parlay_pool[:2],
            "threeLeg": parlay_pool[:3],
        }

    if sport == "wnba":
        pool = [
            *[clone_row(row, row.get("market", "")) for row in data.get("section_boards", {}).get("PTS", {}).get("players", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("section_boards", {}).get("AST", {}).get("players", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("section_boards", {}).get("REB", {}).get("players", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("section_boards", {}).get("3PM", {}).get("players", [])],
        ]
        pool = [row for row in pool if _is_a_tier(row)]
        if not pool:
            return None
        straight = select_daily_rows(pool, "wnba", 1, same_game_max=1, same_team_max=1)
        parlay_pool = select_daily_rows(pool, "wnba", 8, same_game_max=2, same_team_max=1)
        return {
            "straight": straight[0] if straight else None,
            "twoLeg": parlay_pool[:2],
            "threeLeg": parlay_pool[:3],
        }

    if sport == "soccer":
        top = [clone_row(row, data.get("pinned_board", {}).get("market", "GS")) for row in data.get("pinned_board", {}).get("players", [])]
        top = [row for row in top if _is_a_tier(row)]
        if not top:
            return None
        picks = select_daily_rows(top, "soccer", 4, same_game_max=1, same_team_max=1)
        return {"straight": picks[0] if picks else None, "twoLeg": picks[:2], "threeLeg": picks[:3]}

    if sport == "tennis":
        top = [clone_row(row, data.get("pinned_board", {}).get("market", "ML")) for row in data.get("pinned_board", {}).get("players", [])]
        top = [row for row in top if _is_a_tier(row)]
        if not top:
            return None
        picks = select_daily_rows(top, "tennis", 4, same_game_max=1, same_team_max=1)
        return {"straight": picks[0] if picks else None, "twoLeg": picks[:2], "threeLeg": picks[:3]}

    return None


MORNING_REFRESH_HOUR_ET = 8


def _american_to_decimal(american) -> float | None:
    if american is None:
        return None
    s = str(american).strip()
    if not s:
        return None
    try:
        n = int(s.replace("+", ""))
    except ValueError:
        return None
    if n == 0:
        return None
    if n > 0:
        return 1.0 + n / 100.0
    return 1.0 + 100.0 / abs(n)


def _decimal_to_american_str(decimal_odds: float) -> str:
    if decimal_odds <= 1.0:
        return "+0"
    if decimal_odds >= 2.0:
        return f"+{round((decimal_odds - 1.0) * 100)}"
    return f"{round(-100.0 / (decimal_odds - 1.0))}"


def _build_ultimate_cook(sports: dict[str, dict], frozen_at_iso: str) -> dict | None:
    """Mechanical concatenation of every leg already on Picks of the Day.

    Dedupe key: (sport, player_id_or_name, market, line). Daily Edges legs are
    the same rows as per-sport `straight`, so they collapse into the straight
    entry on dedupe.

    TODO: per-leg status (Win/Loss/Push) is not yet emitted by the
    scoring/grading pipeline. All legs and the parlay status default
    to "Pending" until grading writeback exists.
    """
    legs: list[dict] = []
    keyed: dict[tuple, dict] = {}
    for sport_key, board in sports.items():
        ordered: list[tuple[dict, str]] = []
        if board.get("straight"):
            ordered.append((board["straight"], "straight"))
        for row in board.get("twoLeg") or []:
            if row:
                ordered.append((row, "2-leg"))
        for row in board.get("threeLeg") or []:
            if row:
                ordered.append((row, "3-leg"))

        for row, source in ordered:
            ident = row.get("player_id") or row.get("player_name") or ""
            key = (sport_key, ident, row.get("market") or "", row.get("line") or "")
            existing = keyed.get(key)
            if existing:
                if source not in existing["sources"]:
                    existing["sources"].append(source)
                continue
            implied = row.get("implied_odds")
            decimal = _american_to_decimal(implied)
            leg = {
                "sport": sport_key,
                "player_id": row.get("player_id"),
                "player_name": row.get("player_name"),
                "team": row.get("team"),
                "opponent": row.get("opponent"),
                "market": row.get("market"),
                "line": row.get("line"),
                "tier": row.get("tier"),
                "american_odds": str(implied) if implied not in (None, "") else None,
                "decimal_odds": round(decimal, 4) if decimal is not None else None,
                "sources": [source],
                "status": "Pending",
            }
            keyed[key] = leg
            legs.append(leg)

    if not legs:
        return None

    for leg in legs:
        leg["sources"].sort()

    priced_decimals = [leg["decimal_odds"] for leg in legs if leg["decimal_odds"] is not None]
    priced_count = len(priced_decimals)
    total_count = len(legs)
    stake = 10

    if priced_decimals:
        combined_decimal = 1.0
        for d in priced_decimals:
            combined_decimal *= d
        combined_decimal = round(combined_decimal, 4)
        combined_american = _decimal_to_american_str(combined_decimal)
        payout = round(stake * combined_decimal, 2)
        header_summary = (
            f"$10 stake → ${payout:.2f} payout "
            f"(computed from {priced_count} of {total_count} priced legs)"
        )
    else:
        combined_decimal = None
        combined_american = None
        payout = None
        header_summary = (
            f"$10 stake → no priced legs available "
            f"(0 of {total_count} priced legs)"
        )

    return {
        "title": "Ultimate Cook",
        "legs": legs,
        "stake_usd": stake,
        "priced_legs_count": priced_count,
        "total_legs_count": total_count,
        "combined_american_odds": combined_american,
        "combined_decimal_odds": combined_decimal,
        "payout_usd": payout,
        "header_summary": header_summary,
        "status": "Pending",
        "frozen_at": frozen_at_iso,
    }


def write_picks_snapshot(*, boards: dict[str, dict], paths) -> None:
    snapshot_date = local_iso_date()
    final_path = paths.data_final / "picks.json"
    existing = None
    if final_path.exists():
        try:
            import json
            existing = json.loads(final_path.read_text(encoding="utf-8"))
        except Exception:
            existing = None
    # Morning gate: hold the picks until the 8 AM ET refresh so the Morning
    # Card reflects the actual morning slate rather than whatever the cron
    # produced overnight at midnight ET.
    now_et = datetime.now(ET)
    if now_et.hour < MORNING_REFRESH_HOUR_ET:
        return
    if existing and existing.get("date") == snapshot_date:
        return

    sports = {}
    for sport_key, board in boards.items():
        picks = derive_sport_picks(sport_key, board)
        if not picks:
            continue
        # Keep the sport on Picks of the Day if any slot has A-tier rows —
        # don't drop the whole sport just because the top default straight
        # happened to land tier B.
        has_any = picks.get("straight") or picks.get("twoLeg") or picks.get("threeLeg")
        if not has_any:
            continue
        sports[sport_key] = {
            "label": board.get("sport", sport_key.upper()),
            "date": board.get("date"),
            "last_updated": board.get("last_updated"),
            **picks,
        }

    write_moment_utc = datetime.now(ZoneInfo("UTC")).replace(microsecond=0)
    updated_at_iso = write_moment_utc.isoformat().replace("+00:00", "Z")
    payload = {
        "date": snapshot_date,
        "last_updated": format_et_timestamp(datetime.now(ET)),
        "updated_at": updated_at_iso,
        "sports": sports,
    }
    cook = _build_ultimate_cook(sports, updated_at_iso)
    if cook is not None:
        payload["ultimate_cook"] = cook
    write_json(final_path, payload)
    shutil.copy2(final_path, paths.frontend_data / "picks.json")
    shutil.copy2(final_path, paths.pages_data / "picks.json")
