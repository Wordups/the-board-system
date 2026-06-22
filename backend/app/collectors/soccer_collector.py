from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import json
import math
from typing import Any

import requests

from app.outputs.json_writer import write_json
from app.utils.dates import now_et, today_et


HTTP_TIMEOUT = 30
MAX_WORKERS = 8
SOCCER_MARKETS = ["GS", "AST", "SHOTS", "SOT", "BTTS", "1H", "1HML", "OU", "ML"]
# World Cup leads the list so its in-tournament fixtures take priority while it
# is active (Jun-Jul 2026); the domestic leagues are off-season then and only
# serve as fallbacks once club football resumes.
SOCCER_LEAGUES = [
    {"slug": "fifa.world", "label": "FIFA World Cup"},
    {"slug": "eng.1", "label": "Premier League"},
    {"slug": "esp.1", "label": "La Liga"},
    {"slug": "ger.1", "label": "Bundesliga"},
    {"slug": "ita.1", "label": "Serie A"},
    {"slug": "fra.1", "label": "Ligue 1"},
    {"slug": "usa.1", "label": "MLS"},
    {"slug": "uefa.champions", "label": "Champions League"},
]

# National-team rosters on the fifa.world endpoint expose a per-player
# `statistics` block, but it only holds the current World Cup split, which is
# all-zeros before/early in the tournament. To still rank goalscorers we pull
# each rostered player's public athlete overview (club + international splits)
# and aggregate the recent ones into a real scoring profile. These slugs mark
# the leagues whose rosters need that fallback.
OVERVIEW_FALLBACK_LEAGUES = {"fifa.world"}
ATHLETE_OVERVIEW_URL = "https://site.web.api.espn.com/apis/common/v3/sports/soccer/all/athletes/{athlete_id}/overview"
# Only roll up splits from these recent seasons so the profile reflects current
# form rather than a player's entire career.
RECENT_SPLIT_TOKENS = ("2024", "2025", "2026")


def collect_soccer_raw_data(data_raw_dir: Path) -> dict[str, Any]:
    raw_path = data_raw_dir / "soccer_raw.json"
    requested_date = today_et()

    try:
        slate_date, events = fetch_target_soccer_events(requested_date)
        if not events:
            payload = {
                "sport": "SOCCER",
                "date": slate_date.isoformat(),
                "games": [],
            }
            write_json(raw_path, payload)
            return payload

        team_keys = {
            (event["league_slug"], competitor["team"]["id"])
            for event in events
            for competitor in event["competitions"][0]["competitors"]
        }
        rosters = fetch_team_rosters(team_keys)
        recent_form_map = fetch_team_recent_form(team_keys)
        baseline = build_goal_baseline(recent_form_map)
        overview_map = fetch_overview_stats_for_leagues(rosters)

        payload = {
            "sport": "SOCCER",
            "date": slate_date.isoformat(),
            "games": [
                build_game_payload(
                    event=event,
                    rosters=rosters,
                    recent_form_map=recent_form_map,
                    baseline=baseline,
                    overview_map=overview_map,
                )
                for event in events
            ],
        }
        write_json(raw_path, payload)
        return payload
    except Exception:
        if raw_path.exists():
            return json.loads(raw_path.read_text(encoding="utf-8"))
        raise


def fetch_soccer_events(slate_date) -> list[dict[str, Any]]:
    events = []
    seen_ids = set()
    date_token = slate_date.strftime("%Y%m%d")
    for league in SOCCER_LEAGUES:
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league['slug']}/scoreboard"
        try:
            payload = espn_get_json(url, {"dates": date_token})
        except requests.RequestException:
            continue
        for event in payload.get("events", []):
            status = event.get("competitions", [{}])[0].get("status", {}).get("type", {})
            if status.get("completed"):
                continue
            if event["id"] in seen_ids:
                continue
            seen_ids.add(event["id"])
            events.append(
                {
                    **event,
                    "league_slug": league["slug"],
                    "league_label": league["label"],
                }
            )
    return events


def fetch_target_soccer_events(start_date) -> tuple[Any, list[dict[str, Any]]]:
    for offset in range(0, 8):
        slate_date = start_date + timedelta(days=offset)
        events = fetch_soccer_events(slate_date)
        if events:
            return slate_date, events
    return start_date, []


def fetch_team_rosters(team_keys: set[tuple[str, str]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    def load(item: tuple[str, str]) -> tuple[tuple[str, str], list[dict[str, Any]]]:
        league_slug, team_id = item
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/teams/{team_id}/roster"
        payload = espn_get_json(url)
        return item, payload.get("athletes", [])

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, sorted(team_keys)))


def fetch_overview_stats_for_leagues(
    rosters: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, dict[str, float]]:
    """Build {athlete_id: scoring profile} for players whose inline roster
    statistics are unusable (national-team rosters during the World Cup). We
    only fetch overviews for those, deduped by athlete id, in parallel.
    """
    athlete_ids: set[str] = set()
    for (league_slug, _team_id), roster in rosters.items():
        if league_slug not in OVERVIEW_FALLBACK_LEAGUES:
            continue
        for athlete in roster:
            if athlete.get("status", {}).get("type") != "active":
                continue
            if athlete.get("injuries"):
                continue
            position = (athlete.get("position", {}) or {}).get("abbreviation", "")
            if position == "G":  # keepers are not goalscorer/assist candidates
                continue
            athlete_id = str(athlete.get("id", ""))
            if athlete_id:
                athlete_ids.add(athlete_id)

    if not athlete_ids:
        return {}

    def load(athlete_id: str) -> tuple[str, dict[str, float] | None]:
        url = ATHLETE_OVERVIEW_URL.format(athlete_id=athlete_id)
        try:
            payload = espn_get_json(url)
        except requests.RequestException:
            return athlete_id, None
        return athlete_id, aggregate_overview_stats(payload)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = pool.map(load, sorted(athlete_ids))

    return {athlete_id: profile for athlete_id, profile in results if profile}


def aggregate_overview_stats(payload: dict[str, Any]) -> dict[str, float] | None:
    """Roll recent club + international season splits into a single scoring
    profile shaped like flatten_soccer_stats output (appearances, totalGoals,
    goalAssists, shotsOnTarget, totalShots)."""
    statistics = payload.get("statistics") or {}
    names = statistics.get("names") or []
    splits = statistics.get("splits") or []
    if not names or not splits:
        return None

    totals = {"appearances": 0.0, "totalGoals": 0.0, "goalAssists": 0.0, "shotsOnTarget": 0.0, "totalShots": 0.0}
    matched = False
    for split in splits:
        display_name = str(split.get("displayName", ""))
        if not any(token in display_name for token in RECENT_SPLIT_TOKENS):
            continue
        values = dict(zip(names, split.get("stats", [])))
        # `starts` is the closest appearance proxy the overview exposes; it
        # undercounts sub appearances but anchors the per-match denominator.
        totals["appearances"] += parse_number(values.get("starts"))
        totals["totalGoals"] += parse_number(values.get("totalGoals"))
        totals["goalAssists"] += parse_number(values.get("goalAssists"))
        totals["shotsOnTarget"] += parse_number(values.get("shotsOnTarget"))
        totals["totalShots"] += parse_number(values.get("totalShots"))
        matched = True

    if not matched or totals["appearances"] <= 0:
        return None
    return totals


def fetch_team_recent_form(team_keys: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, float]]:
    def load(item: tuple[str, str]) -> tuple[tuple[str, str], dict[str, float]]:
        league_slug, team_id = item
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/teams/{team_id}/schedule"
        payload = espn_get_json(url)
        recent = []
        for event in payload.get("events", []):
            competition = event.get("competitions", [{}])[0]
            status = competition.get("status", {}).get("type", {})
            if not status.get("completed"):
                continue
            recent.append(event)
        recent.sort(key=lambda row: row["date"], reverse=True)
        recent = recent[:5]

        points = goals_for = goals_against = 0.0
        for event in recent:
            competition = event["competitions"][0]
            competitors = competition["competitors"]
            me = next((row for row in competitors if row["team"]["id"] == team_id), None)
            opp = next((row for row in competitors if row["team"]["id"] != team_id), None)
            if not me or not opp:
                continue
            me_score = parse_int(me.get("score"))
            opp_score = parse_int(opp.get("score"))
            goals_for += me_score
            goals_against += opp_score
            if me_score > opp_score:
                points += 3.0
            elif me_score == opp_score:
                points += 1.0

        sample = max(len(recent), 1)
        return item, {
            "points_per_match": points / sample,
            "goals_for_per_match": goals_for / sample,
            "goals_against_per_match": goals_against / sample,
            "sample": len(recent),
        }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        return dict(pool.map(load, sorted(team_keys)))


def build_goal_baseline(recent_form_map: dict[tuple[str, str], dict[str, float]]) -> dict[str, float]:
    values = list(recent_form_map.values())
    return {
        "goals_for": average(profile["goals_for_per_match"] for profile in values) if values else 1.2,
        "goals_against": average(profile["goals_against_per_match"] for profile in values) if values else 1.2,
        "points": average(profile["points_per_match"] for profile in values) if values else 1.4,
    }


def build_game_payload(
    *,
    event: dict[str, Any],
    rosters: dict[tuple[str, str], list[dict[str, Any]]],
    recent_form_map: dict[tuple[str, str], dict[str, float]],
    baseline: dict[str, float],
    overview_map: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    overview_map = overview_map or {}
    competition = event["competitions"][0]
    competitors = competition["competitors"]
    away = next(row for row in competitors if row["homeAway"] == "away")
    home = next(row for row in competitors if row["homeAway"] == "home")
    away_team = away["team"]
    home_team = home["team"]

    away_key = (event["league_slug"], away_team["id"])
    home_key = (event["league_slug"], home_team["id"])
    is_short_tournament = event["league_slug"] == "fifa.world"
    minimum_appearances = 1 if is_short_tournament else 3
    market_odds = (competition.get("odds") or [{}])[0]
    match_profile = estimate_match_profile(
        away_form=recent_form_map.get(away_key, {}),
        home_form=recent_form_map.get(home_key, {}),
        baseline=baseline,
    )
    match_profile = calibrate_match_profile(match_profile, market_odds)

    candidates = []
    candidates.extend(
        build_team_player_candidates(
            game_id=event["id"],
            market_team="away",
            team=away_team,
            opponent=home_team,
            roster=rosters.get(away_key, []),
            team_form=recent_form_map.get(away_key, {}),
            opponent_form=recent_form_map.get(home_key, {}),
            baseline=baseline,
            is_home=False,
            minimum_appearances=minimum_appearances,
            competition_label=event["league_label"],
            team_expected_goals=match_profile["away_xg"],
            overview_map=overview_map,
        )
    )
    candidates.extend(
        build_team_player_candidates(
            game_id=event["id"],
            market_team="home",
            team=home_team,
            opponent=away_team,
            roster=rosters.get(home_key, []),
            team_form=recent_form_map.get(home_key, {}),
            opponent_form=recent_form_map.get(away_key, {}),
            baseline=baseline,
            is_home=True,
            minimum_appearances=minimum_appearances,
            competition_label=event["league_label"],
            team_expected_goals=match_profile["home_xg"],
            overview_map=overview_map,
        )
    )
    candidates.extend(
        build_match_market_candidates(
            game_id=event["id"],
            away_team=away_team,
            home_team=home_team,
            away_form=recent_form_map.get(away_key, {}),
            home_form=recent_form_map.get(home_key, {}),
            baseline=baseline,
            match_profile=match_profile,
            odds=market_odds,
        )
    )

    return {
        "game_id": str(event["id"]),
        "league": event["league_label"],
        "away_team": away_team["abbreviation"],
        "home_team": home_team["abbreviation"],
        "time": competition["status"]["type"].get("shortDetail") or format_event_time(competition["date"]),
        "status": {"phase": translate_state(competition["status"]["type"].get("state"))},
        "candidates": candidates,
    }


def build_team_player_candidates(
    *,
    game_id: str,
    market_team: str,
    team: dict[str, Any],
    opponent: dict[str, Any],
    roster: list[dict[str, Any]],
    team_form: dict[str, float],
    opponent_form: dict[str, float],
    baseline: dict[str, float],
    is_home: bool,
    minimum_appearances: int = 3,
    competition_label: str = "Soccer",
    team_expected_goals: float | None = None,
    overview_map: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    overview_map = overview_map or {}
    players = []
    position_priors = {
        "F": {"goals": 0.28, "assists": 0.14, "shots": 2.0, "sot": 0.82},
        "M": {"goals": 0.10, "assists": 0.16, "shots": 1.25, "sot": 0.38},
        "D": {"goals": 0.04, "assists": 0.07, "shots": 0.55, "sot": 0.14},
    }
    for athlete in roster:
        if athlete.get("status", {}).get("type") != "active":
            continue
        if athlete.get("injuries"):
            continue
        stats = flatten_soccer_stats(athlete.get("statistics", {}))
        appearances = parse_number(stats.get("appearances"))
        # National-team rosters carry an empty WC split; fall back to the
        # player's aggregated club + international overview profile.
        if appearances < 3:
            overview_stats = overview_map.get(str(athlete.get("id", "")))
            if overview_stats:
                stats = overview_stats
                appearances = parse_number(stats.get("appearances"))
        goals = parse_number(stats.get("totalGoals"))
        assists = parse_number(stats.get("goalAssists"))
        shots_on_target = parse_number(stats.get("shotsOnTarget"))
        total_shots = parse_number(stats.get("totalShots"))
        if appearances < minimum_appearances:
            continue

        position = (athlete.get("position", {}) or {}).get("abbreviation", "")
        if position not in position_priors:
            continue
        priors = position_priors[position]
        goals_per_match = goals / max(appearances, 1.0)
        assists_per_match = assists / max(appearances, 1.0)
        sot_per_match = shots_on_target / max(appearances, 1.0)
        shots_per_match = total_shots / max(appearances, 1.0)
        attack_multiplier = clamp_float(
            (team_expected_goals or team_form.get("goals_for_per_match", baseline["goals_for"])) / max(baseline["goals_for"], 0.2),
            0.68,
            1.38,
        )
        venue_multiplier = 1.04 if is_home else 0.98
        multiplier = attack_multiplier * venue_multiplier

        goal_lambda = shrunk_rate(goals_per_match, priors["goals"], appearances, 3.0) * multiplier
        assist_lambda = shrunk_rate(assists_per_match, priors["assists"], appearances, 3.0) * multiplier
        shots_lambda = shrunk_rate(shots_per_match, priors["shots"], appearances, 2.0) * multiplier
        sot_lambda = shrunk_rate(sot_per_match, priors["sot"], appearances, 2.0) * multiplier

        common = {
            "player_id": athlete["id"],
            "player_name": athlete["displayName"],
            "team": team["abbreviation"],
            "opponent": opponent["abbreviation"],
            "game_id": str(game_id),
        }
        sample_note = f"Sample {int(appearances)} | {competition_label} | Shrunk to {position} prior"

        goal_probability = poisson_at_least(goal_lambda, 1)
        if position in {"F", "M"} and goal_probability >= 0.12:
            players.append(probability_candidate(
                **common,
                market="GS",
                line="Anytime Goal",
                probability=goal_probability,
                reason=f"Goal λ {goal_lambda:.2f} | Raw G/m {goals_per_match:.2f} | Team xG {team_expected_goals or 0.0:.2f} | {sample_note}",
            ))

        assist_probability = poisson_at_least(assist_lambda, 1)
        if assist_probability >= 0.10:
            players.append(probability_candidate(
                **common,
                market="AST",
                line="Assist",
                probability=assist_probability,
                reason=f"Assist λ {assist_lambda:.2f} | Raw A/m {assists_per_match:.2f} | Team xG {team_expected_goals or 0.0:.2f} | {sample_note}",
            ))

        shots_line = 2 if shots_lambda >= 1.45 else 1
        shots_probability = poisson_at_least(shots_lambda, shots_line)
        if shots_probability >= 0.34:
            players.append(probability_candidate(
                **common,
                market="SHOTS",
                line=f"{shots_line}+ Shots",
                probability=shots_probability,
                reason=f"Shots λ {shots_lambda:.2f} | Raw shots/m {shots_per_match:.2f} | {sample_note}",
            ))

        sot_line = 2 if sot_lambda >= 1.25 else 1
        sot_probability = poisson_at_least(sot_lambda, sot_line)
        if sot_probability >= 0.28:
            players.append(probability_candidate(
                **common,
                market="SOT",
                line=f"{sot_line}+ Shots on Goal",
                probability=sot_probability,
                reason=f"SOG λ {sot_lambda:.2f} | Raw SOG/m {sot_per_match:.2f} | {sample_note}",
            ))

    return players


def build_match_market_candidates(
    *,
    game_id: str,
    away_team: dict[str, Any],
    home_team: dict[str, Any],
    away_form: dict[str, float],
    home_form: dict[str, float],
    baseline: dict[str, float],
    match_profile: dict[str, float],
    odds: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    odds = odds or {}
    home_abbr = home_team["abbreviation"]
    away_abbr = away_team["abbreviation"]
    home_xg = match_profile["home_xg"]
    away_xg = match_profile["away_xg"]
    total_xg = home_xg + away_xg

    model_ml = poisson_outcome_probabilities(home_xg, away_xg)
    market_ml, ml_odds = extract_three_way_market(odds)
    blended_ml = blend_probabilities(model_ml, market_ml, model_weight=0.58)
    ml_side = max(blended_ml, key=blended_ml.get)
    ml_names = {"home": home_abbr, "away": away_abbr, "draw": "Draw"}
    ml_team = ml_names[ml_side]
    ml_opp = away_abbr if ml_side == "home" else home_abbr if ml_side == "away" else f"{away_abbr}/{home_abbr}"
    model_ml_note = model_ml[ml_side]
    market_ml_note = market_ml.get(ml_side) if market_ml else None

    total_line = float(odds.get("overUnder") or 2.5)
    over_model = poisson_at_least(total_xg, math.floor(total_line) + 1)
    total_market, total_odds = extract_two_way_total(odds)
    over_probability = blend_scalar(over_model, total_market.get("over") if total_market else None, model_weight=0.64)
    under_probability = 1.0 - over_probability
    total_side = "over" if over_probability >= under_probability else "under"
    total_probability = max(over_probability, under_probability)

    btts_yes = (1.0 - math.exp(-home_xg)) * (1.0 - math.exp(-away_xg))
    btts_side = "Yes" if btts_yes >= 0.5 else "No"
    btts_probability = max(btts_yes, 1.0 - btts_yes)

    first_half_share = 0.46
    first_half_total = total_xg * first_half_share
    first_half_goal_probability = 1.0 - math.exp(-first_half_total)
    first_half_ml = poisson_outcome_probabilities(home_xg * first_half_share, away_xg * first_half_share)
    first_half_side = max(first_half_ml, key=first_half_ml.get)
    first_half_names = {"home": home_abbr, "away": away_abbr, "draw": "Draw"}

    common_match = {"game_id": str(game_id)}
    return [
        probability_candidate(
            **common_match,
            player_id=f"{game_id}-ml",
            player_name=ml_team,
            team=ml_team,
            opponent=ml_opp,
            market="ML",
            line="Moneyline" if ml_side != "draw" else "Full Time Draw",
            probability=blended_ml[ml_side],
            implied_odds=ml_odds.get(ml_side),
            reason=f"Calibrated {blended_ml[ml_side]:.1%} | Poisson {model_ml_note:.1%} | Market {market_ml_note:.1%}" if market_ml_note is not None else f"Poisson {model_ml_note:.1%} | xG {home_xg:.2f}-{away_xg:.2f}",
        ),
        probability_candidate(
            **common_match,
            player_id=f"{game_id}-ou",
            player_name="Match Total",
            team=home_abbr,
            opponent=away_abbr,
            market="OU",
            line=f"{total_side.title()} {total_line:g} Goals",
            probability=total_probability,
            implied_odds=total_odds.get(total_side),
            reason=f"Total λ {total_xg:.2f} | Model over {over_model:.1%} | Calibrated {total_side} {total_probability:.1%}",
        ),
        probability_candidate(
            **common_match,
            player_id=f"{game_id}-btts",
            player_name="Both Teams to Score",
            team=home_abbr,
            opponent=away_abbr,
            market="BTTS",
            line=f"BTTS {btts_side}",
            probability=btts_probability,
            reason=f"Home score {1-math.exp(-home_xg):.1%} | Away score {1-math.exp(-away_xg):.1%} | xG {home_xg:.2f}-{away_xg:.2f}",
        ),
        probability_candidate(
            **common_match,
            player_id=f"{game_id}-1h",
            player_name="First Half Goal",
            team=home_abbr,
            opponent=away_abbr,
            market="1H",
            line="1H Over 0.5 Goals",
            probability=first_half_goal_probability,
            reason=f"First-half λ {first_half_total:.2f} | 46% goal-time share | Full-match xG {total_xg:.2f}",
        ),
        probability_candidate(
            **common_match,
            player_id=f"{game_id}-1hml",
            player_name=first_half_names[first_half_side],
            team=first_half_names[first_half_side],
            opponent=ml_opp,
            market="1HML",
            line="1H Moneyline" if first_half_side != "draw" else "1H Draw",
            probability=first_half_ml[first_half_side],
            reason=f"1H Poisson {first_half_ml[first_half_side]:.1%} | 1H xG {home_xg*first_half_share:.2f}-{away_xg*first_half_share:.2f}",
        ),
    ]


def estimate_match_profile(*, away_form: dict[str, float], home_form: dict[str, float], baseline: dict[str, float]) -> dict[str, float]:
    home_sample = max(0.0, float(home_form.get("sample", 0)))
    away_sample = max(0.0, float(away_form.get("sample", 0)))
    base_for = max(0.2, baseline["goals_for"])
    base_against = max(0.2, baseline["goals_against"])
    home_attack = shrunk_rate(home_form.get("goals_for_per_match", base_for), base_for, home_sample, 3.0)
    away_attack = shrunk_rate(away_form.get("goals_for_per_match", base_for), base_for, away_sample, 3.0)
    home_defense = shrunk_rate(home_form.get("goals_against_per_match", base_against), base_against, home_sample, 3.0)
    away_defense = shrunk_rate(away_form.get("goals_against_per_match", base_against), base_against, away_sample, 3.0)
    home_xg = clamp_float(((home_attack + away_defense) / 2.0) * 1.06, 0.18, 3.8)
    away_xg = clamp_float(((away_attack + home_defense) / 2.0) * 0.97, 0.16, 3.5)
    return {"home_xg": home_xg, "away_xg": away_xg}


def calibrate_match_profile(profile: dict[str, float], odds: dict[str, Any]) -> dict[str, float]:
    """Calibrate form xG to de-vigged 1X2 and total prices when available."""
    market_ml, _ = extract_three_way_market(odds)
    total_market, _ = extract_two_way_total(odds)
    if not market_ml or not total_market:
        return profile

    total_line = float(odds.get("overUnder") or 2.5)
    market_total = solve_poisson_total(total_line, total_market["over"])
    form_total = profile["home_xg"] + profile["away_xg"]
    calibrated_total = form_total * 0.35 + market_total * 0.65

    best_home = calibrated_total / 2.0
    best_loss = float("inf")
    # A small deterministic grid is stable and more than precise enough for UI probabilities.
    for step in range(1, 240):
        home_rate = 0.05 + (calibrated_total - 0.10) * (step / 240.0)
        away_rate = calibrated_total - home_rate
        if away_rate <= 0.05:
            continue
        outcomes = poisson_outcome_probabilities(home_rate, away_rate)
        loss = sum((outcomes[key] - market_ml[key]) ** 2 for key in outcomes)
        if loss < best_loss:
            best_loss = loss
            best_home = home_rate

    market_home = best_home
    market_away = calibrated_total - best_home
    form_home_share = profile["home_xg"] / max(form_total, 1e-9)
    form_home = calibrated_total * form_home_share
    form_away = calibrated_total - form_home
    home_xg = market_home * 0.82 + form_home * 0.18
    away_xg = market_away * 0.82 + form_away * 0.18
    return {"home_xg": clamp_float(home_xg, 0.12, 4.2), "away_xg": clamp_float(away_xg, 0.12, 4.0)}


def solve_poisson_total(line: float, target_over_probability: float) -> float:
    threshold = math.floor(line) + 1
    low, high = 0.15, 7.0
    for _ in range(60):
        middle = (low + high) / 2.0
        if poisson_at_least(middle, threshold) < target_over_probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def probability_candidate(
    *,
    player_id: str,
    player_name: str,
    team: str,
    opponent: str,
    game_id: str,
    market: str,
    line: str,
    probability: float,
    reason: str,
    implied_odds: Any = None,
) -> dict[str, Any]:
    probability = clamp_float(probability, 0.01, 0.99)
    score = round(probability * 100.0, 2)
    row = {
        "player_id": str(player_id),
        "player_name": player_name,
        "team": team,
        "opponent": opponent,
        "game_id": str(game_id),
        "market": market,
        "line": line,
        "score": score,
        "confidence": clamp_int(score),
        "tier": assign_soccer_tier(score),
        "reason": reason,
        "sim_prob_pct": score,
        "model_hit_rate": round(probability, 4),
    }
    if implied_odds not in (None, ""):
        row["implied_odds"] = str(implied_odds)
    return row


def poisson_at_least(rate: float, threshold: int) -> float:
    if threshold <= 0:
        return 1.0
    rate = max(0.0, rate)
    below = sum(math.exp(-rate) * (rate ** k) / math.factorial(k) for k in range(threshold))
    return clamp_float(1.0 - below)


def poisson_outcome_probabilities(home_rate: float, away_rate: float, max_goals: int = 10) -> dict[str, float]:
    home = draw = away = 0.0
    for home_goals in range(max_goals + 1):
        home_p = math.exp(-home_rate) * (home_rate ** home_goals) / math.factorial(home_goals)
        for away_goals in range(max_goals + 1):
            away_p = math.exp(-away_rate) * (away_rate ** away_goals) / math.factorial(away_goals)
            joint = home_p * away_p
            if home_goals > away_goals:
                home += joint
            elif home_goals < away_goals:
                away += joint
            else:
                draw += joint
    total = max(home + draw + away, 1e-9)
    return {"home": home / total, "draw": draw / total, "away": away / total}


def american_probability(value: Any) -> float | None:
    try:
        odds = int(str(value).replace("+", ""))
    except (TypeError, ValueError):
        return None
    if odds == 0:
        return None
    return 100.0 / (odds + 100.0) if odds > 0 else abs(odds) / (abs(odds) + 100.0)


def extract_three_way_market(odds: dict[str, Any]) -> tuple[dict[str, float] | None, dict[str, Any]]:
    moneyline = odds.get("moneyline", {})
    prices = {side: moneyline.get(side, {}).get("close", {}).get("odds") for side in ("home", "draw", "away")}
    raw = {side: american_probability(value) for side, value in prices.items()}
    if any(value is None for value in raw.values()):
        return None, prices
    total = sum(raw.values())
    return ({side: value / total for side, value in raw.items()}, prices)


def extract_two_way_total(odds: dict[str, Any]) -> tuple[dict[str, float] | None, dict[str, Any]]:
    total_market = odds.get("total", {})
    prices = {side: total_market.get(side, {}).get("close", {}).get("odds") for side in ("over", "under")}
    raw = {side: american_probability(value) for side, value in prices.items()}
    if any(value is None for value in raw.values()):
        return None, prices
    total = sum(raw.values())
    return ({side: value / total for side, value in raw.items()}, prices)


def blend_probabilities(model: dict[str, float], market: dict[str, float] | None, *, model_weight: float) -> dict[str, float]:
    if not market:
        return model
    blended = {key: model[key] * model_weight + market[key] * (1.0 - model_weight) for key in model}
    total = sum(blended.values())
    return {key: value / total for key, value in blended.items()}


def blend_scalar(model: float, market: float | None, *, model_weight: float) -> float:
    if market is None:
        return model
    return model * model_weight + market * (1.0 - model_weight)


def shrunk_rate(observed: float, prior: float, sample: float, prior_weight: float) -> float:
    sample = max(0.0, sample)
    return (observed * sample + prior * prior_weight) / max(sample + prior_weight, 1e-9)


def flatten_soccer_stats(stat_blob: dict[str, Any]) -> dict[str, float]:
    flat: dict[str, float] = {}
    for category in stat_blob.get("splits", {}).get("categories", []):
        for stat in category.get("stats", []):
            flat[stat["name"]] = parse_number(stat.get("value"))
    return flat


def assign_soccer_tier(score: float) -> str:
    if score >= 72:
        return "A"
    if score >= 56:
        return "B"
    return "C"


def translate_state(state: str | None) -> str:
    if state == "post":
        return "final"
    if state == "in":
        return "live"
    return "pregame"


def format_event_time(value: str) -> str:
    event_time = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(now_et().tzinfo)
    return event_time.strftime("%I:%M %p ET").lstrip("0")


def espn_get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(url, params=params, headers={"User-Agent": "the-board-system/1.0"}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def parse_number(value: Any) -> float:
    if isinstance(value, dict):
        return parse_number(value.get("value") if "value" in value else value.get("displayValue"))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_int(value: Any) -> int:
    if isinstance(value, dict):
        return parse_int(value.get("value") if "value" in value else value.get("displayValue"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def average(values) -> float:
    values = list(values)
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def normalize_rate(value: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return max(0.0, min(value / scale, 1.0))


def clamp_int(value: float) -> int:
    return max(1, min(99, round(value)))


def clamp_score(value: float) -> float:
    return max(1.0, min(99.0, value))


def clamp_float(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, float(value)))
