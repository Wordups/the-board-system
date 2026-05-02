from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import urllib.parse
import urllib.request
from typing import Any

from app.outputs.json_writer import write_json
from app.schemas.player_schema import RawPlayerMarketInput
from app.utils.dates import now_et, today_et


STATS_API_BASE = "https://statsapi.mlb.com/api/v1"
REQUEST_TIMEOUT = 20
MAX_WORKERS = 8


def collect_mlb_raw_data(data_raw_dir: Path) -> dict[str, Any]:
    slate_date = today_et().isoformat()
    season = int(slate_date[:4])

    try:
        schedule_games = fetch_schedule(slate_date)
        if not schedule_games:
            raise RuntimeError(f"No MLB games found for {slate_date}")

        game_boxscores = fetch_game_boxscores(schedule_games)

        team_ids = sorted(
            {
                game["teams"]["away"]["team"]["id"]
                for game in schedule_games
            }
            | {
                game["teams"]["home"]["team"]["id"]
                for game in schedule_games
            }
        )
        rosters = fetch_team_rosters(team_ids, season)
        team_hitting_stats = fetch_team_hitting_stats(team_ids, season)

        payload = {
            "sport": "MLB",
            "date": slate_date,
            "games": [
                build_game_payload(
                    game=game,
                    rosters=rosters,
                    team_hitting_stats=team_hitting_stats,
                    game_boxscore=game_boxscores.get(game["gamePk"]),
                    season=season,
                    slate_date=slate_date,
                )
                for game in schedule_games
            ],
        }
    except Exception:
        raw_path = data_raw_dir / "mlb_raw.json"
        if raw_path.exists():
            return json.loads(raw_path.read_text(encoding="utf-8"))
        raise

    write_json(data_raw_dir / "mlb_raw.json", payload)
    return payload


def fetch_schedule(slate_date: str) -> list[dict[str, Any]]:
    params = {
        "sportId": 1,
        "date": slate_date,
        "hydrate": "probablePitcher,team,linescore",
    }
    data = fetch_json(f"{STATS_API_BASE}/schedule?{urllib.parse.urlencode(params)}")
    if not data.get("dates"):
        return []
    return data["dates"][0].get("games", [])


def fetch_team_rosters(team_ids: list[int], season: int) -> dict[int, dict[str, Any]]:
    def load(team_id: int) -> tuple[int, dict[str, Any]]:
        params = {
            "rosterType": "active",
            "hydrate": f"person(stats(type=[season,gameLog,career,yearByYear],group=[hitting,pitching],season={season}))",
        }
        url = f"{STATS_API_BASE}/teams/{team_id}/roster?{urllib.parse.urlencode(params)}"
        return team_id, fetch_json(url)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, team_ids))


def fetch_team_hitting_stats(team_ids: list[int], season: int) -> dict[int, dict[str, Any]]:
    def load(team_id: int) -> tuple[int, dict[str, Any]]:
        params = {"stats": "season", "group": "hitting", "season": season}
        url = f"{STATS_API_BASE}/teams/{team_id}/stats?{urllib.parse.urlencode(params)}"
        stats = fetch_json(url)["stats"][0]["splits"][0]["stat"]
        return team_id, stats

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, team_ids))


def fetch_game_boxscores(schedule_games: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    live_or_final = [
        game for game in schedule_games
        if build_game_status(game).get("phase") in {"live", "final"}
    ]

    def load(game: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        url = f"{STATS_API_BASE}/game/{game['gamePk']}/boxscore"
        return game["gamePk"], fetch_json(url)

    if not live_or_final:
        return {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, live_or_final))


def build_game_payload(
    *,
    game: dict[str, Any],
    rosters: dict[int, dict[str, Any]],
    team_hitting_stats: dict[int, dict[str, Any]],
    game_boxscore: dict[str, Any] | None,
    season: int,
    slate_date: str,
) -> dict[str, Any]:
    away = game["teams"]["away"]
    home = game["teams"]["home"]
    away_team = away["team"]
    home_team = home["team"]
    away_abbr = team_abbreviation(away_team)
    home_abbr = team_abbreviation(home_team)

    players = []
    players.extend(
        build_hitter_inputs(
            roster=rosters[away_team["id"]],
            team_abbr=away_abbr,
            opponent_abbr=home_abbr,
            game_id=f"{away_abbr.lower()}-{home_abbr.lower()}-{slate_date}",
            opposing_pitcher=home.get("probablePitcher"),
        )
    )
    players.extend(
        build_hitter_inputs(
            roster=rosters[home_team["id"]],
            team_abbr=home_abbr,
            opponent_abbr=away_abbr,
            game_id=f"{away_abbr.lower()}-{home_abbr.lower()}-{slate_date}",
            opposing_pitcher=away.get("probablePitcher"),
        )
    )
    players.extend(
        build_pitcher_inputs(
            probable_pitcher=away.get("probablePitcher"),
            roster=rosters[away_team["id"]],
            team_abbr=away_abbr,
            opponent_abbr=home_abbr,
            game_id=f"{away_abbr.lower()}-{home_abbr.lower()}-{slate_date}",
            opponent_team_hitting=team_hitting_stats[home_team["id"]],
            season=season,
        )
    )
    players.extend(
        build_pitcher_inputs(
            probable_pitcher=home.get("probablePitcher"),
            roster=rosters[home_team["id"]],
            team_abbr=home_abbr,
            opponent_abbr=away_abbr,
            game_id=f"{away_abbr.lower()}-{home_abbr.lower()}-{slate_date}",
            opponent_team_hitting=team_hitting_stats[away_team["id"]],
            season=season,
        )
    )
    players.extend(
        build_moneyline_inputs(
            away_team=away_team,
            away_record=away.get("leagueRecord", {}),
            home_team=home_team,
            home_record=home.get("leagueRecord", {}),
            game_id=f"{away_abbr.lower()}-{home_abbr.lower()}-{slate_date}",
            home_abbr=home_abbr,
            away_abbr=away_abbr,
        )
    )

    return {
        "game_id": f"{away_abbr.lower()}-{home_abbr.lower()}-{slate_date}",
        "away_team": away_abbr,
        "home_team": home_abbr,
        "time": format_game_time(game["gameDate"]),
        "game_date": game["gameDate"],
        "status": build_game_status(game),
        "player_hr_results": build_player_hr_results(game_boxscore, build_game_status(game)),
        "players": [player.model_dump() for player in players],
    }


def build_hitter_inputs(
    *,
    roster: dict[str, Any],
    team_abbr: str,
    opponent_abbr: str,
    game_id: str,
    opposing_pitcher: dict[str, Any] | None,
) -> list[RawPlayerMarketInput]:
    season = season_from_game_id(game_id)
    batters = []
    for roster_entry in roster.get("roster", []):
        if roster_entry["position"]["abbreviation"] == "P":
            continue
        person = roster_entry["person"]
        season_stats = get_stat_split(person, group="hitting", stat_type="season")
        if not season_stats:
            continue
        batters.append((roster_entry, person, season_stats))

    batters.sort(
        key=lambda item: (
            parse_int(item[2].get("plateAppearances")),
            parse_decimal(item[2].get("ops")),
            parse_decimal(item[2].get("slg")),
        ),
        reverse=True,
    )
    top_batters = batters[:9]
    pitcher_matchup = pitcher_matchup_value(opposing_pitcher)

    results: list[RawPlayerMarketInput] = []
    for index, (_, person, season_stats) in enumerate(top_batters, start=1):
        game_logs = get_stat_split(person, group="hitting", stat_type="gameLog", fallback=[])
        recent_5 = game_logs[:5]
        recent_10 = game_logs[:10]

        season_games = max(parse_int(season_stats.get("gamesPlayed")), 1)
        season_pa = max(parse_int(season_stats.get("plateAppearances")), 1)
        season_hr = parse_int(season_stats.get("homeRuns"))
        season_hits = parse_int(season_stats.get("hits"))
        season_tb = parse_int(season_stats.get("totalBases"))
        avg = parse_decimal(season_stats.get("avg"))
        ops = parse_decimal(season_stats.get("ops"))
        slg = parse_decimal(season_stats.get("slg"))
        age = parse_int(season_stats.get("age")) or parse_int(person.get("currentAge"))
        iso = max(slg - avg, 0.0)
        hr_per_game = season_hr / season_games
        hits_per_game = season_hits / season_games
        tb_per_game = season_tb / season_games

        season_pa_per_game = season_pa / season_games
        season_hr_rate = smoothed_rate(season_hr, season_pa, prior_rate=0.032, stabilization=90)
        history_metrics = historical_hr_metrics(person, current_season=season)

        recent_5_pa = sum(parse_int(log["stat"].get("plateAppearances")) for log in recent_5)
        recent_10_pa = sum(parse_int(log["stat"].get("plateAppearances")) for log in recent_10)
        recent_5_hr_total = sum(parse_int(log["stat"].get("homeRuns")) for log in recent_5)
        recent_10_hr_total = sum(parse_int(log["stat"].get("homeRuns")) for log in recent_10)
        l5_hr = sum(parse_int(log["stat"].get("homeRuns")) for log in recent_5) / max(len(recent_5), 1)
        l10_hr = sum(parse_int(log["stat"].get("homeRuns")) for log in recent_10) / max(len(recent_10), 1)
        l5_hits = sum(parse_int(log["stat"].get("hits")) for log in recent_5) / max(len(recent_5), 1)
        l10_hits = sum(parse_int(log["stat"].get("hits")) for log in recent_10) / max(len(recent_10), 1)
        l5_tb = sum(parse_int(log["stat"].get("totalBases")) for log in recent_5) / max(len(recent_5), 1)
        l10_tb = sum(parse_int(log["stat"].get("totalBases")) for log in recent_10) / max(len(recent_10), 1)

        l5_hr_rate = smoothed_rate(recent_5_hr_total, recent_5_pa, prior_rate=season_hr_rate, stabilization=12)
        l10_hr_rate = smoothed_rate(recent_10_hr_total, recent_10_pa, prior_rate=season_hr_rate, stabilization=24)

        lineup_boost = max(0.0, (10 - index) / 10.0)
        playing_time = clamp(season_pa_per_game / 4.4, 0.45, 1.0)
        form_boost = clamp((ops - 0.680) / 0.450, 0.0, 1.0)
        power_boost = clamp((iso - 0.140) / 0.180, 0.0, 1.0)
        sample_reliability = clamp(season_pa / 180.0, 0.25, 1.0)
        projected_pa = clamp(3.15 + playing_time * 0.95 + lineup_boost * 0.55, 3.2, 4.8)

        season_hr_chance = probability_of_event(season_hr_rate, projected_pa)
        l10_hr_chance = probability_of_event(l10_hr_rate, projected_pa)
        l5_hr_chance = probability_of_event(l5_hr_rate, projected_pa)
        historical_hr_chance = probability_of_event(history_metrics["historical_hr_rate"], projected_pa)
        hr_skill = clamp(
            season_hr_chance * 0.52 + l10_hr_chance * 0.28 + l5_hr_chance * 0.20,
            0.0,
            1.0,
        )
        power_history_boost = clamp((historical_hr_chance - 0.14) / 0.18, 0.0, 1.0)
        unlucky_power_gap = clamp((historical_hr_chance - l10_hr_chance) / 0.12, 0.0, 1.0)
        unlucky_power_signal = unlucky_power_gap * clamp(power_boost * 0.65 + form_boost * 0.35, 0.0, 1.0)
        rising_star_signal = rising_star_index(
            age=age,
            season_pa=season_pa,
            season_hr_chance=season_hr_chance,
            historical_hr_chance=historical_hr_chance,
            iso=iso,
            ops=ops,
            lineup_boost=lineup_boost,
            sample_reliability=sample_reliability,
        )
        adjusted_hr_value = clamp(
            hr_skill * (0.68 + sample_reliability * 0.24)
            + power_boost * 0.05
            + power_history_boost * 0.07
            + unlucky_power_signal * 0.05,
            0.0,
            1.0,
        )
        adjusted_hr_value = clamp(
            adjusted_hr_value + rising_star_signal * 0.05,
            0.0,
            1.0,
        )

        results.append(
            RawPlayerMarketInput(
                player_id=str(person["id"]),
                player_name=person["fullName"],
                team=team_abbr,
                opponent=opponent_abbr,
                game_id=game_id,
                market="HR",
                line="HR 1+",
                stat_value=adjusted_hr_value,
                baseline=0.12,
                trend=clamp(l5_hr_chance * 0.45 + l10_hr_chance * 0.35 + historical_hr_chance * 0.20, 0.0, 1.0),
                matchup=clamp(pitcher_matchup * 0.44 + power_boost * 0.28 + power_history_boost * 0.18 + lineup_boost * 0.10, 0.0, 1.0),
                recent_form=clamp(form_boost * 0.22 + power_boost * 0.24 + playing_time * 0.25 + unlucky_power_signal * 0.18 + rising_star_signal * 0.11, 0.0, 1.0),
                extra={
                    "age": age,
                    "season_hr_per_game": round(hr_per_game, 3),
                    "l5_hr_per_game": round(l5_hr, 3),
                    "l10_hr_per_game": round(l10_hr, 3),
                    "season_hr_probability": round(season_hr_chance, 3),
                    "l5_hr_probability": round(l5_hr_chance, 3),
                    "l10_hr_probability": round(l10_hr_chance, 3),
                    "historical_hr_probability": round(historical_hr_chance, 3),
                    "ops": round(ops, 3),
                    "slg": round(slg, 3),
                    "iso": round(iso, 3),
                    "sample_reliability": round(sample_reliability, 3),
                    "projected_pa": round(projected_pa, 2),
                    "order_estimate": index,
                    "career_hr_rate": round(history_metrics["career_hr_rate"], 3),
                    "recent_peak_hr_rate": round(history_metrics["recent_peak_hr_rate"], 3),
                    "historical_power_index": round(power_history_boost, 3),
                    "unlucky_power_index": round(unlucky_power_signal, 3),
                    "rising_star_index": round(rising_star_signal, 3),
                    "pitcher_matchup": round(pitcher_matchup, 3),
                },
            )
        )
        results.append(
            RawPlayerMarketInput(
                player_id=f'{person["id"]}-tb',
                player_name=person["fullName"],
                team=team_abbr,
                opponent=opponent_abbr,
                game_id=game_id,
                market="TB",
                line="2+ TB",
                stat_value=clamp(0.45 * normalize_rate(tb_per_game, 4.0) + 0.35 * normalize_rate(l10_tb, 4.0) + 0.20 * normalize_rate(l5_tb, 4.0), 0.0, 1.0),
                baseline=0.28,
                trend=clamp(normalize_rate(l5_tb, 4.0), 0.0, 1.0),
                matchup=clamp(pitcher_matchup * 0.65 + slg * 0.35, 0.0, 1.0),
                recent_form=clamp(form_boost * 0.5 + avg * 0.5, 0.0, 1.0),
            )
        )
        results.append(
            RawPlayerMarketInput(
                player_id=f'{person["id"]}-hits',
                player_name=person["fullName"],
                team=team_abbr,
                opponent=opponent_abbr,
                game_id=game_id,
                market="Hits",
                line="1+ Hit" if l5_hits < 1.4 else "2+ Hits",
                stat_value=clamp(0.45 * hits_per_game + 0.35 * l10_hits + 0.20 * l5_hits, 0.0, 1.0),
                baseline=0.42,
                trend=clamp(l5_hits / 2.0, 0.0, 1.0),
                matchup=clamp(pitcher_matchup * 0.5 + avg * 0.5, 0.0, 1.0),
                recent_form=clamp(avg * 0.6 + form_boost * 0.4, 0.0, 1.0),
            )
        )

    return results


def build_pitcher_inputs(
    *,
    probable_pitcher: dict[str, Any] | None,
    roster: dict[str, Any],
    team_abbr: str,
    opponent_abbr: str,
    game_id: str,
    opponent_team_hitting: dict[str, Any],
    season: int,
) -> list[RawPlayerMarketInput]:
    if not probable_pitcher:
        return []

    person = find_person_in_roster(roster, probable_pitcher["id"])
    if person is None:
        person = fetch_person(probable_pitcher["id"], season)

    season_stats = get_stat_split(person, group="pitching", stat_type="season")
    game_logs = get_stat_split(person, group="pitching", stat_type="gameLog", fallback=[])
    if not season_stats:
        return []

    recent_5 = game_logs[:5]
    recent_10 = game_logs[:10]
    season_games = max(parse_int(season_stats.get("gamesStarted")) or parse_int(season_stats.get("gamesPlayed")), 1)
    innings = innings_to_float(season_stats.get("inningsPitched"))
    strikeouts = parse_int(season_stats.get("strikeOuts"))

    season_k_avg = strikeouts / season_games
    season_ip_avg = innings / season_games
    l5_k_avg = average(parse_int(log["stat"].get("strikeOuts")) for log in recent_5)
    l10_k_avg = average(parse_int(log["stat"].get("strikeOuts")) for log in recent_10)
    l5_ip_avg = average(innings_to_float(log["stat"].get("inningsPitched")) for log in recent_5)
    era = parse_decimal(season_stats.get("era"))
    whip = parse_decimal(season_stats.get("whip"))

    opp_k_rate = normalize_rate(
        parse_int(opponent_team_hitting.get("strikeOuts")),
        max(parse_int(opponent_team_hitting.get("plateAppearances")), 1),
    )
    pitch_quality = clamp(((5.00 - era) / 4.5) * 0.45 + ((1.45 - whip) / 0.7) * 0.35 + normalize_rate(l5_ip_avg, 7.0) * 0.20, 0.0, 1.0)
    line_guess = max(4, min(9, round(max(l5_k_avg, season_k_avg))))

    return [
        RawPlayerMarketInput(
            player_id=str(person["id"]),
            player_name=person["fullName"],
            team=team_abbr,
            opponent=opponent_abbr,
            game_id=game_id,
            market="K",
            line=f"{line_guess}+ K",
            stat_value=clamp(0.45 * normalize_rate(season_k_avg, 10.0) + 0.35 * normalize_rate(l10_k_avg, 10.0) + 0.20 * normalize_rate(l5_k_avg, 10.0), 0.0, 1.0),
            baseline=0.40,
            trend=clamp(normalize_rate(l5_k_avg, 10.0), 0.0, 1.0),
            matchup=clamp(opp_k_rate * 1.3, 0.0, 1.0),
            recent_form=clamp(pitch_quality, 0.0, 1.0),
        )
    ]


def build_moneyline_inputs(
    *,
    away_team: dict[str, Any],
    away_record: dict[str, Any],
    home_team: dict[str, Any],
    home_record: dict[str, Any],
    game_id: str,
    home_abbr: str,
    away_abbr: str,
) -> list[RawPlayerMarketInput]:
    away_pct = parse_decimal(away_record.get("pct"))
    home_pct = parse_decimal(home_record.get("pct"))

    home_edge = clamp(home_pct + 0.035, 0.0, 1.0)
    away_edge = clamp(away_pct - 0.015, 0.0, 1.0)
    pick_home = home_edge >= away_edge
    pick_team = home_team if pick_home else away_team
    pick_abbr = home_abbr if pick_home else away_abbr
    opp_abbr = away_abbr if pick_home else home_abbr
    pick_value = home_edge if pick_home else away_edge

    return [
        RawPlayerMarketInput(
            player_id=f"{pick_team['id']}-moneyline",
            player_name=pick_team["name"],
            team=pick_abbr,
            opponent=opp_abbr,
            game_id=game_id,
            market="ML",
            line="Moneyline",
            stat_value=clamp(pick_value, 0.0, 1.0),
            baseline=0.50,
            trend=clamp(abs(home_pct - away_pct), 0.0, 1.0),
            matchup=0.10 if pick_home else 0.05,
            recent_form=clamp(pick_value, 0.0, 1.0),
        )
    ]


def fetch_person(person_id: int, season: int) -> dict[str, Any]:
    params = {"hydrate": f"stats(type=[season,gameLog],group=[pitching],season={season})"}
    url = f"{STATS_API_BASE}/people/{person_id}?{urllib.parse.urlencode(params)}"
    data = fetch_json(url)
    return data["people"][0]


def find_person_in_roster(roster: dict[str, Any], person_id: int) -> dict[str, Any] | None:
    for row in roster.get("roster", []):
        person = row["person"]
        if person["id"] == person_id:
            return person
    return None


def get_stat_split(
    person: dict[str, Any],
    *,
    group: str,
    stat_type: str,
    fallback: Any = None,
) -> Any:
    for stats_blob in person.get("stats", []):
        if stats_blob["group"]["displayName"].lower() != group.lower():
            continue
        if stats_blob["type"]["displayName"].lower() != stat_type.lower():
            continue
        splits = stats_blob.get("splits", [])
        if stat_type.lower() == "season":
            return splits[0]["stat"] if splits else fallback
        return splits if splits else fallback
    return fallback


def season_from_game_id(game_id: str) -> int:
    try:
        return int(str(game_id).rsplit("-", 3)[-3])
    except (TypeError, ValueError, IndexError):
        return today_et().year


def historical_hr_metrics(person: dict[str, Any], *, current_season: int) -> dict[str, float]:
    career_stats_raw = get_stat_split(person, group="hitting", stat_type="career", fallback=[]) or []
    if isinstance(career_stats_raw, list):
        career_stats = (career_stats_raw[0] or {}).get("stat", {}) if career_stats_raw else {}
    else:
        career_stats = career_stats_raw or {}
    career_hr = parse_int(career_stats.get("homeRuns"))
    career_pa = parse_int(career_stats.get("plateAppearances"))
    career_hr_rate = smoothed_rate(career_hr, career_pa, prior_rate=0.032, stabilization=180)

    year_by_year = get_stat_split(person, group="hitting", stat_type="yearByYear", fallback=[]) or []
    recent_rates: list[float] = []
    recent_weighted_total = 0.0
    recent_weight = 0.0
    seasons_considered = 0
    for split in sorted(year_by_year, key=lambda row: parse_int(row.get("season")), reverse=True):
        season = parse_int(split.get("season"))
        if season <= 0 or season >= current_season:
            continue
        stat = split.get("stat", {})
        pa = parse_int(stat.get("plateAppearances"))
        hr = parse_int(stat.get("homeRuns"))
        if pa < 120:
            continue
        rate = smoothed_rate(hr, pa, prior_rate=career_hr_rate, stabilization=60)
        weight = max(0.25, 1.0 - seasons_considered * 0.18)
        recent_rates.append(rate)
        recent_weighted_total += rate * weight
        recent_weight += weight
        seasons_considered += 1
        if seasons_considered == 3:
            break

    recent_peak_hr_rate = max(recent_rates, default=career_hr_rate)
    recent_avg_hr_rate = (recent_weighted_total / recent_weight) if recent_weight else career_hr_rate
    historical_hr_rate = clamp(career_hr_rate * 0.40 + recent_avg_hr_rate * 0.35 + recent_peak_hr_rate * 0.25, 0.0, 1.0)

    return {
        "career_hr_rate": career_hr_rate,
        "recent_peak_hr_rate": recent_peak_hr_rate,
        "recent_avg_hr_rate": recent_avg_hr_rate,
        "historical_hr_rate": historical_hr_rate,
    }


def rising_star_index(
    *,
    age: int,
    season_pa: int,
    season_hr_chance: float,
    historical_hr_chance: float,
    iso: float,
    ops: float,
    lineup_boost: float,
    sample_reliability: float,
) -> float:
    if age <= 0 or age > 27:
        return 0.0
    age_boost = clamp((27 - age) / 5.0, 0.0, 1.0)
    runway = clamp((900 - season_pa) / 900.0, 0.0, 1.0)
    breakout_gap = clamp((season_hr_chance - historical_hr_chance) / 0.10, 0.0, 1.0)
    power_quality = clamp((iso - 0.165) / 0.145, 0.0, 1.0)
    overall_quality = clamp((ops - 0.760) / 0.220, 0.0, 1.0)
    trust = clamp(sample_reliability, 0.30, 1.0)
    return clamp(
        age_boost * 0.24
        + runway * 0.16
        + breakout_gap * 0.24
        + power_quality * 0.18
        + overall_quality * 0.10
        + lineup_boost * 0.04
        + trust * 0.04,
        0.0,
        1.0,
    )


def pitcher_matchup_value(probable_pitcher: dict[str, Any] | None) -> float:
    if not probable_pitcher:
        return 0.45
    era = parse_decimal(probable_pitcher.get("era"))
    if not era:
        return 0.45
    return clamp((era - 2.80) / 4.00, 0.15, 0.90)


def team_abbreviation(team: dict[str, Any]) -> str:
    if team.get("abbreviation"):
        return team["abbreviation"]
    lookup = fetch_json(f"{STATS_API_BASE}/teams/{team['id']}")
    return lookup["teams"][0]["abbreviation"]


def format_game_time(game_date: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    first_pitch = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
    local = first_pitch.astimezone(ZoneInfo("America/New_York"))
    return local.strftime("%I:%M %p ET").lstrip("0")


def build_game_status(game: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime

    status = game.get("status", {})
    first_pitch = datetime.fromisoformat(game["gameDate"].replace("Z", "+00:00")).astimezone(now_et().tzinfo)
    now = now_et()
    detailed = status.get("detailedState", "")
    abstract = status.get("abstractGameState", "")
    coded = status.get("codedGameState", "")
    linescore = game.get("linescore", {})
    current_inning = parse_int(linescore.get("currentInning"))

    if abstract == "Final" or coded == "F":
        phase = "final"
    elif abstract == "Live" or coded in {"M", "I"}:
        phase = "live"
    else:
        phase = "pregame"

    minutes_to_start = int((first_pitch - now).total_seconds() // 60)
    is_lineup_window = phase == "pregame" and minutes_to_start <= 90
    probable_pitchers_confirmed = bool(
        game.get("teams", {}).get("away", {}).get("probablePitcher")
        and game.get("teams", {}).get("home", {}).get("probablePitcher")
    )

    return {
        "phase": phase,
        "abstract_state": abstract,
        "detailed_state": detailed,
        "coded_state": coded,
        "start_time_et": first_pitch.isoformat(),
        "minutes_to_start": minutes_to_start,
        "current_inning": current_inning,
        "is_lineup_window": is_lineup_window,
        "probable_pitchers_confirmed": probable_pitchers_confirmed,
    }


def build_player_hr_results(game_boxscore: dict[str, Any] | None, status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not game_boxscore:
        return {}

    phase = status.get("phase", "pregame")
    results: dict[str, dict[str, Any]] = {}
    for side in ("away", "home"):
        for player_blob in game_boxscore.get("teams", {}).get(side, {}).get("players", {}).values():
            person = player_blob.get("person", {})
            player_id = str(person.get("id") or "")
            if not player_id:
                continue
            batting = player_blob.get("stats", {}).get("batting", {})
            home_runs = parse_int(batting.get("homeRuns"))
            result = "pending"
            if home_runs > 0:
                result = "hit"
            elif phase == "final":
                result = "miss"
            results[player_id] = {
                "result": result,
                "home_runs": home_runs,
            }
    return results


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "the-board-system/1.0"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.load(response)


def parse_decimal(value: Any) -> float:
    if value in (None, "", "-.--", ".---"):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def average(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def normalize_rate(value: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return clamp(value / scale, 0.0, 1.0)


def innings_to_float(value: Any) -> float:
    if value in (None, "", "-.--"):
        return 0.0
    raw = str(value)
    if "." not in raw:
        return parse_decimal(raw)
    whole, frac = raw.split(".", 1)
    mapping = {"0": 0.0, "1": 1.0 / 3.0, "2": 2.0 / 3.0}
    return parse_decimal(whole) + mapping.get(frac, 0.0)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def smoothed_rate(count: int, opportunities: int, *, prior_rate: float, stabilization: int) -> float:
    opportunities = max(opportunities, 0)
    stabilization = max(stabilization, 0)
    total_opportunities = opportunities + stabilization
    if total_opportunities <= 0:
        return clamp(prior_rate, 0.0, 1.0)
    return clamp((count + prior_rate * stabilization) / total_opportunities, 0.0, 1.0)


def probability_of_event(rate: float, opportunities: float) -> float:
    opportunities = max(opportunities, 0.0)
    if rate <= 0.0 or opportunities <= 0.0:
        return 0.0
    return clamp(1.0 - ((1.0 - rate) ** opportunities), 0.0, 1.0)
