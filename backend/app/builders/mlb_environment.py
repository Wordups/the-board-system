from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests


# Hardcoded park reference data. HR factors are approximate multi-year
# averages (FanGraphs / BR park-factor reports), 1.00 = neutral. CF bearings
# are degrees from home plate to dead center field on a standard compass
# (0 = N, 90 = E, 180 = S, 270 = W). "dome" includes retractable-roof parks
# since wind effect is negligible whenever the roof is closed; the chip
# treats them as roofed outright rather than guess at the daily roof state.
PARKS: dict[str, dict[str, Any]] = {
    "ARI": {"name": "Chase Field",         "lat": 33.4453, "lon": -112.0667, "cf_deg":  25, "dome": True,  "hr_lhb": 1.05, "hr_rhb": 1.03},
    "ATL": {"name": "Truist Park",         "lat": 33.8908, "lon": -84.4678,  "cf_deg":  80, "dome": False, "hr_lhb": 0.97, "hr_rhb": 1.02},
    "BAL": {"name": "Camden Yards",        "lat": 39.2839, "lon": -76.6217,  "cf_deg":  30, "dome": False, "hr_lhb": 1.05, "hr_rhb": 1.02},
    "BOS": {"name": "Fenway Park",         "lat": 42.3467, "lon": -71.0972,  "cf_deg":  40, "dome": False, "hr_lhb": 0.95, "hr_rhb": 1.10},
    "CHC": {"name": "Wrigley Field",       "lat": 41.9484, "lon": -87.6553,  "cf_deg":  30, "dome": False, "hr_lhb": 1.06, "hr_rhb": 1.04},
    "CWS": {"name": "Rate Field",          "lat": 41.8300, "lon": -87.6339,  "cf_deg":  45, "dome": False, "hr_lhb": 1.07, "hr_rhb": 1.05},
    "CIN": {"name": "Great American Ball Park", "lat": 39.0972, "lon": -84.5070, "cf_deg": 33, "dome": False, "hr_lhb": 1.18, "hr_rhb": 1.16},
    "CLE": {"name": "Progressive Field",   "lat": 41.4961, "lon": -81.6852,  "cf_deg":  10, "dome": False, "hr_lhb": 0.92, "hr_rhb": 0.93},
    "COL": {"name": "Coors Field",         "lat": 39.7559, "lon": -104.9942, "cf_deg":  10, "dome": False, "hr_lhb": 1.30, "hr_rhb": 1.28},
    "DET": {"name": "Comerica Park",       "lat": 42.3390, "lon": -83.0485,  "cf_deg":  30, "dome": False, "hr_lhb": 0.94, "hr_rhb": 0.92},
    "HOU": {"name": "Minute Maid Park",    "lat": 29.7572, "lon": -95.3552,  "cf_deg":   9, "dome": True,  "hr_lhb": 1.05, "hr_rhb": 1.03},
    "KC":  {"name": "Kauffman Stadium",    "lat": 39.0517, "lon": -94.4803,  "cf_deg":   6, "dome": False, "hr_lhb": 0.86, "hr_rhb": 0.88},
    "LAA": {"name": "Angel Stadium",       "lat": 33.8003, "lon": -117.8827, "cf_deg":  50, "dome": False, "hr_lhb": 1.00, "hr_rhb": 1.02},
    "LAD": {"name": "Dodger Stadium",      "lat": 34.0739, "lon": -118.2400, "cf_deg":  25, "dome": False, "hr_lhb": 1.02, "hr_rhb": 1.02},
    "MIA": {"name": "loanDepot park",      "lat": 25.7781, "lon": -80.2197,  "cf_deg":  30, "dome": True,  "hr_lhb": 0.78, "hr_rhb": 0.78},
    "MIL": {"name": "American Family Field", "lat": 43.0280, "lon": -87.9712, "cf_deg": 60, "dome": True,  "hr_lhb": 1.03, "hr_rhb": 1.05},
    "MIN": {"name": "Target Field",        "lat": 44.9817, "lon": -93.2776,  "cf_deg":  80, "dome": False, "hr_lhb": 1.00, "hr_rhb": 0.98},
    "NYM": {"name": "Citi Field",          "lat": 40.7571, "lon": -73.8458,  "cf_deg":  78, "dome": False, "hr_lhb": 0.92, "hr_rhb": 0.94},
    "NYY": {"name": "Yankee Stadium",      "lat": 40.8296, "lon": -73.9262,  "cf_deg":  75, "dome": False, "hr_lhb": 1.18, "hr_rhb": 1.05},
    "ATH": {"name": "Sutter Health Park",  "lat": 38.5803, "lon": -121.5070, "cf_deg":   0, "dome": False, "hr_lhb": 1.10, "hr_rhb": 1.05},
    "PHI": {"name": "Citizens Bank Park",  "lat": 39.9061, "lon": -75.1665,  "cf_deg":  25, "dome": False, "hr_lhb": 1.10, "hr_rhb": 1.10},
    "PIT": {"name": "PNC Park",            "lat": 40.4469, "lon": -80.0058,  "cf_deg": 120, "dome": False, "hr_lhb": 0.85, "hr_rhb": 0.90},
    "SD":  {"name": "Petco Park",          "lat": 32.7073, "lon": -117.1573, "cf_deg":  50, "dome": False, "hr_lhb": 0.90, "hr_rhb": 0.92},
    "SEA": {"name": "T-Mobile Park",       "lat": 47.5914, "lon": -122.3325, "cf_deg":  25, "dome": True,  "hr_lhb": 0.94, "hr_rhb": 0.96},
    "SF":  {"name": "Oracle Park",         "lat": 37.7786, "lon": -122.3893, "cf_deg": 110, "dome": False, "hr_lhb": 0.80, "hr_rhb": 0.95},
    "STL": {"name": "Busch Stadium",       "lat": 38.6226, "lon": -90.1928,  "cf_deg":  80, "dome": False, "hr_lhb": 0.93, "hr_rhb": 0.95},
    "TB":  {"name": "Tropicana Field",     "lat": 27.7682, "lon": -82.6534,  "cf_deg": 120, "dome": True,  "hr_lhb": 0.95, "hr_rhb": 0.97},
    "TEX": {"name": "Globe Life Field",    "lat": 32.7473, "lon": -97.0817,  "cf_deg":  30, "dome": True,  "hr_lhb": 1.05, "hr_rhb": 1.05},
    "TOR": {"name": "Rogers Centre",       "lat": 43.6414, "lon": -79.3894,  "cf_deg":  20, "dome": True,  "hr_lhb": 1.00, "hr_rhb": 1.00},
    "WSH": {"name": "Nationals Park",      "lat": 38.8730, "lon": -77.0074,  "cf_deg":  30, "dome": False, "hr_lhb": 1.00, "hr_rhb": 0.99},
}

# Aliases — sometimes the data feed uses old codes.
PARK_ALIASES: dict[str, str] = {
    "OAK": "ATH",  # Oakland → Athletics (West Sacramento, 2025–2027)
    "AZ":  "ARI",
    "CHW": "CWS",
    "KCR": "KC",
    "SDP": "SD",
    "SFG": "SF",
    "TBR": "TB",
    "WSN": "WSH",
}

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 8


def host_team_from_game_id(game_id: str) -> str | None:
    """game_id format: 'wsh-mia-2026-05-10' (away-home-date). Home = 2nd token."""
    if not game_id or "-" not in game_id:
        return None
    parts = game_id.split("-")
    if len(parts) < 2:
        return None
    return parts[1].upper()


def host_team_from_matchup(matchup: str) -> str | None:
    """matchup format: 'WSH @ MIA' (away @ home). Home = right side."""
    if not matchup or "@" not in matchup:
        return None
    return matchup.split("@")[-1].strip().upper()


def resolve_park(team: str | None) -> dict[str, Any] | None:
    if not team:
        return None
    code = team.strip().upper()
    code = PARK_ALIASES.get(code, code)
    return PARKS.get(code)


def fetch_wind(lat: float, lon: float) -> dict[str, Any] | None:
    """Open-Meteo current-weather pull. No API key required. Returns
    None on any failure so the pipeline never breaks because the
    weather service had a bad moment."""
    try:
        r = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "wind_speed_10m,wind_direction_10m",
                "wind_speed_unit": "mph",
                "timezone": "America/New_York",
            },
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        cur = (r.json() or {}).get("current") or {}
        speed = cur.get("wind_speed_10m")
        deg = cur.get("wind_direction_10m")
        if speed is None or deg is None:
            return None
        return {"mph": round(float(speed)), "deg": round(float(deg))}
    except Exception:
        return None


def wind_label(wind_deg: float, cf_deg: float) -> str:
    """Classify wind as OUT / IN / CROSS relative to the park's
    home-plate-to-CF axis. wind_deg is the compass direction the wind
    is COMING FROM (meteorological convention). For wind to push balls
    toward CF (OUT), it must originate behind home plate, i.e. from
    (cf_deg + 180) mod 360."""
    coming_from = float(wind_deg) % 360
    push_toward_cf_from = (float(cf_deg) + 180.0) % 360
    delta = abs((coming_from - push_toward_cf_from + 540) % 360 - 180)
    if delta <= 45:
        return "OUT"
    if delta >= 135:
        return "IN"
    return "CROSS"


def hr_lean(park: dict, wlabel: str | None) -> str:
    """Combine park HR factor + wind direction into a single lean.
    Returns 'HR boost', 'HR suppress', or 'neutral'."""
    avg_factor = (park["hr_lhb"] + park["hr_rhb"]) / 2.0
    score = (avg_factor - 1.0) * 10.0
    if not park["dome"] and wlabel == "OUT":
        score += 1.0
    elif not park["dome"] and wlabel == "IN":
        score -= 1.0
    if score >= 0.6:
        return "HR boost"
    if score <= -0.6:
        return "HR suppress"
    return "neutral"


def _build_env_for_game(game: dict) -> dict[str, Any] | None:
    host = (
        host_team_from_game_id(game.get("game_id") or "")
        or host_team_from_matchup(game.get("matchup") or "")
    )
    park = resolve_park(host)
    if not park:
        return None

    if park["dome"]:
        wlabel: str | None = "DOME"
        wind_obj: dict[str, Any] | None = None
    else:
        wind_obj = fetch_wind(park["lat"], park["lon"])
        wlabel = wind_label(wind_obj["deg"], park["cf_deg"]) if wind_obj else None

    lean = hr_lean(park, wlabel)

    if park["dome"]:
        chip = f'{park["name"]} · DOME'
    elif wind_obj is None:
        chip = f'{park["name"]} · wind n/a'
    else:
        chip = f'{park["name"]} · {wind_obj["mph"]}mph {wlabel}'
    if lean != "neutral" and not park["dome"]:
        chip += f' ({lean})'
    elif lean != "neutral" and park["dome"]:
        # Dome — wind irrelevant, but park itself can still lean
        chip += f' ({lean})'

    return {
        "park_name": park["name"],
        "host_team": host,
        "is_dome": park["dome"],
        "park_factor_lhb": park["hr_lhb"],
        "park_factor_rhb": park["hr_rhb"],
        "wind_mph": wind_obj["mph"] if wind_obj else None,
        "wind_deg": wind_obj["deg"] if wind_obj else None,
        "wind_label": wlabel,
        "hr_lean": lean,
        "summary_chip": chip,
    }


def enrich_board_with_environment(board: dict) -> None:
    """Mutate board in place: add `env` to each game in board['games'].

    Designed to never break the pipeline — any per-game failure (unknown
    park code, weather API hiccup) simply leaves that game without an
    env field. The frontend renders the chip only when env.summary_chip
    exists, so missing data degrades silently to the existing card.

    Display-only addition — does NOT touch scoring, tiers, or any field
    consumed by backend/app/scoring/. Hard Non-Goal #1 holds.
    """
    games = board.get("games") or []
    if not games:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        envs = list(pool.map(_build_env_for_game, games))
    for game, env in zip(games, envs):
        if env:
            game["env"] = env
