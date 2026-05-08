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
    for key in ("implied_odds", "value_zone", "edge", "model_hit_rate"):
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


def derive_sport_picks(sport: str, data: dict) -> dict | None:
    if not data or data.get("date") != local_iso_date():
        return None

    if sport == "mlb":
        hr = data.get("daily_hr_picks", {})
        return {
            "straight": clone_row(hr.get("single"), "HR"),
            "twoLeg": [clone_row(row, "HR") for row in (hr.get("two_leg", {}).get("legs") or []) if row],
            "threeLeg": [clone_row(row, "HR") for row in (hr.get("three_leg", {}).get("legs") or []) if row],
        }

    if sport == "nba":
        pool = [
            *[clone_row(row, row.get("market", "")) for row in data.get("research_board", {}).get("safe_plays", {}).get("players", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("research_board", {}).get("sections", {}).get("PTS", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("research_board", {}).get("sections", {}).get("AST", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("research_board", {}).get("sections", {}).get("REB", [])],
            *[clone_row(row, row.get("market", "")) for row in data.get("research_board", {}).get("sections", {}).get("3PM", [])],
        ]
        pool = [row for row in pool if row]
        straight = select_daily_rows(pool, "nba", 1, same_game_max=1, same_team_max=1)
        parlay_pool = select_daily_rows(pool, "nba", 8, same_game_max=2, same_team_max=1)
        return {
            "straight": straight[0] if straight else clone_row(data.get("hero_pick"), data.get("hero_pick", {}).get("market", "")),
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
        pool = [row for row in pool if row]
        straight = select_daily_rows(pool, "wnba", 1, same_game_max=1, same_team_max=1)
        parlay_pool = select_daily_rows(pool, "wnba", 8, same_game_max=2, same_team_max=1)
        return {
            "straight": straight[0] if straight else clone_row(data.get("hero_pick"), data.get("hero_pick", {}).get("market", "")),
            "twoLeg": parlay_pool[:2],
            "threeLeg": parlay_pool[:3],
        }

    if sport == "soccer":
        top = [clone_row(row, data.get("pinned_board", {}).get("market", "GS")) for row in data.get("pinned_board", {}).get("players", [])]
        top = [row for row in top if row]
        picks = select_daily_rows(top, "soccer", 4, same_game_max=1, same_team_max=1)
        return {"straight": picks[0] if picks else None, "twoLeg": picks[:2], "threeLeg": picks[:3]}

    if sport == "tennis":
        top = [clone_row(row, data.get("pinned_board", {}).get("market", "ML")) for row in data.get("pinned_board", {}).get("players", [])]
        top = [row for row in top if row]
        picks = select_daily_rows(top, "tennis", 4, same_game_max=1, same_team_max=1)
        return {"straight": picks[0] if picks else None, "twoLeg": picks[:2], "threeLeg": picks[:3]}

    return None


MORNING_REFRESH_HOUR_ET = 8


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
        if picks and picks.get("straight"):
            sports[sport_key] = {
                "label": board.get("sport", sport_key.upper()),
                "date": board.get("date"),
                "last_updated": board.get("last_updated"),
                **picks,
            }

    payload = {
        "date": snapshot_date,
        "last_updated": format_et_timestamp(datetime.now(ET)),
        "sports": sports,
    }
    write_json(final_path, payload)
    shutil.copy2(final_path, paths.frontend_data / "picks.json")
    shutil.copy2(final_path, paths.pages_data / "picks.json")
