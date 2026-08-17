"""NFL model — projections, value lines, and candidate scoring.

Category 1 of the NFL board: **pure predictions**. Nothing in this module
consults salary, contract value, or any other money signal — it reads game
logs and opponent allowances and quotes a line. The salary work lives in
``app.builders.nfl_salary_board`` and is deliberately downstream of this, so a
contract file that is stale or missing cannot move a prediction.

Line selection follows the same value-pricing discipline as NBA/WNBA: search
the player's plausible range and ship the line whose shrunken hit rate lands
closest to 0.50, rather than the line engineered to look like a lock. Football
just needs a coarser grid — books quote passing yards in 25-yard steps, not
one-yard steps — so the search is stepped rather than unit-by-unit.
"""

from __future__ import annotations

from typing import Any

from app.collectors.nfl_collector import average
from app.scoring.lineups import compute_team_lineup_context, is_playable, lineup_summary_note
from app.scoring.value import bayesian_hit_rate, format_implied_odds, hit_rate_to_implied_odds, value_zone


NFL_MARKETS = ["PASS_YDS", "RUSH_YDS", "REC_YDS", "REC", "TD", "ML"]

# Which positions are quotable in which market. A quarterback's rushing line is
# a real market; a quarterback's reception line is not.
MARKET_POSITIONS: dict[str, set[str]] = {
    "PASS_YDS": {"QB"},
    "RUSH_YDS": {"RB", "QB"},
    "REC_YDS": {"WR", "TE", "RB"},
    "REC": {"WR", "TE", "RB"},
    "TD": {"RB", "WR", "TE"},
}

# Grid the line search walks, matching the alternate lines books actually post.
NFL_LINE_STEPS = {"PASS_YDS": 25, "RUSH_YDS": 10, "REC_YDS": 10, "REC": 1, "TD": 1}
NFL_LINE_MINIMUMS = {"PASS_YDS": 150, "RUSH_YDS": 20, "REC_YDS": 20, "REC": 2, "TD": 1}

# Minimum projected production before a market is quoted at all — keeps deep
# backups off the board instead of quoting a 20-yard receiving line for a WR5.
MARKET_PROJECTION_FLOOR = {"PASS_YDS": 140.0, "RUSH_YDS": 18.0, "REC_YDS": 18.0, "REC": 1.6, "TD": 0.12}

# Recency weights. Football's 17-game season means the current-season sample is
# always small, so the season average carries more weight than it does in a
# sport with 82 games.
RECENCY_WEIGHTS = {"l5": 0.40, "l10": 0.24, "season": 0.24, "vs_opp": 0.12}

# Score bonus per value zone — same intent as the NBA table.
NFL_VALUE_ZONE_BONUS = {"aim": 14.0, "value": 12.0, "lean": 4.0, "longshot": 6.0}

# Home-field production nudge per market.
HOME_BOOST = {"PASS_YDS": 6.0, "RUSH_YDS": 2.0, "REC_YDS": 2.5, "REC": 0.15, "TD": 0.02}

MIN_GAMES_FOR_QUOTE = 3


# ------------------------------------------------------------------ top level


def build_nfl_candidates(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every scored candidate on the slate, across all games and markets."""
    profiles_by_team: dict[str, list[dict[str, Any]]] = {}
    for profile in raw_payload.get("player_profiles", {}).values():
        profiles_by_team.setdefault(profile["team"], []).append(profile)

    defense_profiles = raw_payload.get("defense_profiles", {})
    baselines = raw_payload.get("allowance_baselines", {})

    candidates: list[dict[str, Any]] = []
    for game in raw_payload.get("games", []):
        home, away = game["home_team"], game["away_team"]
        spread = game.get("spread")
        for team, opponent, is_home in ((home, away, True), (away, home, False)):
            candidates.extend(
                build_team_candidates(
                    game=game,
                    team=team,
                    opponent=opponent,
                    is_home=is_home,
                    # `spread` arrives from the home team's perspective; flip it
                    # so every candidate carries its own team's number, which is
                    # what the simulator's game-script conditioning expects.
                    team_spread=None if spread is None else (float(spread) if is_home else -float(spread)),
                    profiles=profiles_by_team.get(team, []),
                    opponent_defense=defense_profiles.get(opponent, {}),
                    baselines=baselines,
                )
            )
        candidates.extend(
            build_moneyline_candidates(
                game=game,
                defense_profiles=defense_profiles,
            )
        )
    return candidates


def build_team_candidates(
    *,
    game: dict[str, Any],
    team: str,
    opponent: str,
    is_home: bool,
    team_spread: float | None,
    profiles: list[dict[str, Any]],
    opponent_defense: dict[str, float],
    baselines: dict[str, float],
) -> list[dict[str, Any]]:
    if not profiles:
        return []

    lineup_input = [
        {
            "player_name": profile["player_name"],
            "injury_status": profile.get("injury_status", "ACTIVE"),
            "usage_load": profile.get("snap_load", 0.0),
        }
        for profile in profiles
    ]
    lineup_context = compute_team_lineup_context(lineup_input)
    boost_factor = float(lineup_context.get("boost_factor", 1.0))
    star_outs = list(lineup_context.get("star_outs", []))

    active = [profile for profile in profiles if is_playable(profile.get("injury_status", "ACTIVE"))]
    team_snap_total = sum(float(profile.get("snap_load", 0.0)) for profile in active) or 1.0

    candidates: list[dict[str, Any]] = []
    for profile in active:
        status = profile.get("injury_status", "ACTIVE")
        opportunity_share = min(
            (float(profile.get("snap_load", 0.0)) / team_snap_total) * boost_factor,
            0.60,
        )
        for market in ("PASS_YDS", "RUSH_YDS", "REC_YDS", "REC", "TD"):
            if profile["position"] not in MARKET_POSITIONS[market]:
                continue
            candidate = build_market_candidate(
                market=market,
                game=game,
                team=team,
                opponent=opponent,
                is_home=is_home,
                team_spread=team_spread,
                profile=profile,
                opportunity_share=opportunity_share,
                opponent_defense=opponent_defense,
                baselines=baselines,
            )
            if not candidate:
                continue
            candidate["lineup_status"] = status
            candidate["team_star_outs"] = star_outs
            candidate["team_star_gtd"] = [
                name for name in lineup_context.get("star_gtd", []) if name != profile["player_name"]
            ]
            candidate["team_usage_boost"] = round(boost_factor, 3)
            candidate["team_lost_usage"] = round(float(lineup_context.get("lost_usage", 0.0)), 2)
            note = lineup_summary_note(lineup_context, status)
            if note:
                candidate["reason"] = f"{candidate['reason']} | {note}"
            candidates.append(candidate)
    return candidates


# ---------------------------------------------------------------- projections


def project_market(
    *,
    market: str,
    profile: dict[str, Any],
    opponent: str,
    is_home: bool,
    matchup_ratio: float,
) -> dict[str, float]:
    """Blend recency windows into one projection, then apply the matchup."""
    logs = profile["logs"]
    recent_10 = logs[:10]
    recent_5 = logs[:5]
    season_avg = float(profile["season_avgs"].get(market, 0.0))
    l10_avg = average(log.get(market, 0.0) for log in recent_10)
    l5_avg = average(log.get(market, 0.0) for log in recent_5)
    vs_opp_logs = [log for log in logs if log.get("opponent") == opponent][:6]
    vs_opp_avg = average(log.get(market, 0.0) for log in vs_opp_logs) if vs_opp_logs else season_avg

    blended = (
        (l5_avg * RECENCY_WEIGHTS["l5"])
        + (l10_avg * RECENCY_WEIGHTS["l10"])
        + (season_avg * RECENCY_WEIGHTS["season"])
        + (vs_opp_avg * RECENCY_WEIGHTS["vs_opp"])
    )
    if is_home:
        blended += HOME_BOOST.get(market, 0.0)
    projection = blended * matchup_ratio

    return {
        "projection": projection,
        "season_avg": season_avg,
        "l10_avg": l10_avg,
        "l5_avg": l5_avg,
        "vs_opp_avg": vs_opp_avg,
        "vs_opp_games": float(len(vs_opp_logs)),
        "trend_delta": l5_avg - season_avg,
    }


def matchup_ratio_for(*, market: str, opponent_defense: dict[str, float], baselines: dict[str, float]) -> float:
    """How generous this defense has been, relative to the league.

    Clamped to [0.85, 1.15]: five games of NFL data is a small sample and an
    uncapped ratio would let one blowout dominate a projection.
    """
    if market == "PASS_YDS" or market in {"REC_YDS", "REC"}:
        allowed, baseline = opponent_defense.get("allowed_pass_yds", 0.0), baselines.get("pass_yds", 0.0)
    elif market == "RUSH_YDS":
        allowed, baseline = opponent_defense.get("allowed_rush_yds", 0.0), baselines.get("rush_yds", 0.0)
    else:  # TD tracks points allowed
        allowed, baseline = opponent_defense.get("allowed_points", 0.0), baselines.get("points", 0.0)
    if not baseline or not allowed:
        return 1.0
    return max(0.85, min(1.15, allowed / baseline))


def find_value_line(
    *,
    market: str,
    recent_logs: list[dict[str, Any]],
    baseline: float,
    projection: float,
) -> dict[str, Any] | None:
    """Stepped line search — the football analogue of ``scoring.value.find_value_line``.

    Returns the rung whose Bayesian-shrunken hit rate sits closest to 0.50,
    tie-broken toward the more positive projection edge.
    """
    if not recent_logs:
        return None
    step = NFL_LINE_STEPS[market]
    minimum = NFL_LINE_MINIMUMS[market]
    floor = max(minimum, round_to_step(min(baseline, projection) * 0.6, step))
    ceiling = max(floor + step, round_to_step(projection + step * 2, step))

    n = len(recent_logs)
    candidates: list[dict[str, Any]] = []
    line = floor
    while line <= ceiling:
        hits = sum(1 for log in recent_logs if float(log.get(market, 0.0)) >= line)
        p = bayesian_hit_rate(hits, n, prior_hit_rate=0.50, prior_strength=4)
        candidates.append(
            {
                "line": line,
                "hit_rate": round(p, 4),
                "raw_hit_rate": round(hits / n, 4),
                "implied_odds": hit_rate_to_implied_odds(p),
                "edge": round(projection - line, 2),
                "zone": value_zone(p),
            }
        )
        line += step

    if not candidates:
        return None
    return min(candidates, key=lambda row: (abs(row["hit_rate"] - 0.50), -row["edge"]))


def round_to_step(value: float, step: int) -> int:
    if step <= 1:
        return max(1, int(round(value)))
    return max(step, int(round(value / step)) * step)


# ------------------------------------------------------------------ candidates


def build_market_candidate(
    *,
    market: str,
    game: dict[str, Any],
    team: str,
    opponent: str,
    is_home: bool,
    team_spread: float | None,
    profile: dict[str, Any],
    opportunity_share: float,
    opponent_defense: dict[str, float],
    baselines: dict[str, float],
) -> dict[str, Any] | None:
    logs = profile["logs"]
    recent_10 = logs[:10]
    if len(recent_10) < MIN_GAMES_FOR_QUOTE:
        return None

    matchup_ratio = matchup_ratio_for(market=market, opponent_defense=opponent_defense, baselines=baselines)
    projected = project_market(
        market=market,
        profile=profile,
        opponent=opponent,
        is_home=is_home,
        matchup_ratio=matchup_ratio,
    )
    projection = projected["projection"]
    if projection < MARKET_PROJECTION_FLOOR[market]:
        return None

    if market == "TD":
        valued = build_td_line(recent_logs=recent_10, projection=projection)
    else:
        baseline = max(projected["season_avg"], projected["l10_avg"], 0.0) or projection
        valued = find_value_line(
            market=market,
            recent_logs=recent_10,
            baseline=baseline,
            projection=projection,
        )
    if not valued:
        return None

    model_hit_rate = valued["hit_rate"]
    zone = valued["zone"]
    vs_opp_logs = [log for log in logs if log.get("opponent") == opponent][:6]
    vs_opp_hit_rate = (
        sum(1 for log in vs_opp_logs if float(log.get(market, 0.0)) >= valued["line"]) / len(vs_opp_logs)
        if vs_opp_logs
        else model_hit_rate
    )

    score = score_candidate(
        model_hit_rate=model_hit_rate,
        zone=zone,
        opportunity_share=opportunity_share,
        matchup_ratio=matchup_ratio,
        trend_delta=projected["trend_delta"],
        season_avg=projected["season_avg"],
        vs_opp_hit_rate=vs_opp_hit_rate,
        vs_opp_games=len(vs_opp_logs),
        is_home=is_home,
        sample_size=len(recent_10),
    )
    tier = classify_tier(
        zone=zone,
        opportunity_share=opportunity_share,
        matchup_ratio=matchup_ratio,
        sample_size=len(recent_10),
    )

    return {
        "player_id": str(profile["player_id"]),
        "player_name": profile["player_name"],
        "player_key": profile.get("player_key", ""),
        "position": profile["position"],
        "team": team,
        "opponent": opponent,
        "game_id": game["game_id"],
        "market": market,
        "line": format_market_line(market, valued["line"]),
        "score": round(score, 2),
        "confidence": max(1, min(99, round(score))),
        "tier": tier,
        "implied_odds": format_implied_odds(valued["implied_odds"]),
        "implied_odds_value": valued["implied_odds"],
        "value_zone": zone,
        "edge": valued["edge"],
        "model_hit_rate": round(model_hit_rate, 3),
        # The simulator reads these two as the empirical clear rates, and
        # spread / over_under / indoor as game-script conditioning.
        "l10_hit_rate": round(model_hit_rate, 3),
        "l5_hit_rate": round(valued.get("raw_hit_rate", model_hit_rate), 3),
        "vs_opp_hit_rate": round(vs_opp_hit_rate, 3),
        "spread": team_spread,
        "over_under": game.get("over_under"),
        "indoor": game.get("indoor", False),
        "opportunity_share": round(opportunity_share, 3),
        "projection": round(projection, 2),
        "matchup_ratio": round(matchup_ratio, 3),
        "sample_size": len(recent_10),
        "reason": build_reason(
            market=market,
            valued=valued,
            projected=projected,
            matchup_ratio=matchup_ratio,
            opportunity_share=opportunity_share,
            opponent=opponent,
            vs_opp_hit_rate=vs_opp_hit_rate,
            vs_opp_games=len(vs_opp_logs),
            is_home=is_home,
            sample_size=len(recent_10),
        ),
    }


def build_td_line(*, recent_logs: list[dict[str, Any]], projection: float) -> dict[str, Any]:
    """Anytime touchdown — one rung, so there is nothing to search.

    The quoted probability is the shrunken rate of games with a scrimmage TD.
    """
    n = len(recent_logs)
    hits = sum(1 for log in recent_logs if float(log.get("TD", 0.0)) >= 1)
    p = bayesian_hit_rate(hits, n, prior_hit_rate=0.35, prior_strength=4)
    return {
        "line": 1,
        "hit_rate": round(p, 4),
        "raw_hit_rate": round(hits / n, 4) if n else 0.0,
        "implied_odds": hit_rate_to_implied_odds(p),
        "edge": round(projection - 0.5, 2),
        "zone": value_zone(p),
    }


def score_candidate(
    *,
    model_hit_rate: float,
    zone: str,
    opportunity_share: float,
    matchup_ratio: float,
    trend_delta: float,
    season_avg: float,
    vs_opp_hit_rate: float,
    vs_opp_games: int,
    is_home: bool,
    sample_size: int,
) -> float:
    """0-100 composite. Peaks near the AIM zone, rewards volume and matchup."""
    aim_proximity = max(0.0, 1.0 - abs(model_hit_rate - 0.50) * 2.0)
    opportunity_component = min(opportunity_share / 0.30, 1.0)
    matchup_component = min(max((matchup_ratio - 0.90) / 0.20, 0.0), 1.0)
    # Trend is scale-free: a 15-yard bump means something different to a
    # 300-yard passer than to a 40-yard receiver.
    scale = max(season_avg, 1.0)
    trend_component = min(max((trend_delta / scale + 0.25) / 0.50, 0.0), 1.0)
    h2h_confidence = min(vs_opp_games / 3.0, 1.0)
    h2h_component = min(max((vs_opp_hit_rate - 0.50) / 0.40, 0.0), 1.0) * h2h_confidence
    home_component = 1.0 if is_home else 0.0

    score = 100 * (
        (aim_proximity * 0.30)
        + (opportunity_component * 0.20)
        + (matchup_component * 0.15)
        + (trend_component * 0.10)
        + (h2h_component * 0.05)
        + (home_component * 0.05)
    )
    score += NFL_VALUE_ZONE_BONUS.get(zone, 0.0)
    if sample_size < 5:
        # Small-sample penalty — early-season logs and returning-from-injury
        # players shouldn't rank alongside a full body of work.
        score -= 6.0
    if opportunity_share < 0.10:
        score -= 5.0
    return max(score, 1.0)


def classify_tier(*, zone: str, opportunity_share: float, matchup_ratio: float, sample_size: int) -> str:
    playable_zones = {"aim", "value", "longshot"}
    if zone in playable_zones and opportunity_share >= 0.22 and matchup_ratio >= 1.02 and sample_size >= 5:
        return "A"
    if zone in playable_zones and opportunity_share >= 0.14 and sample_size >= 4:
        return "B"
    if zone == "lean" and opportunity_share >= 0.20 and sample_size >= 5:
        return "B"
    return "C"


def format_market_line(market: str, line: int) -> str:
    return f"{line}+ {market.replace('_', ' ')}"


def build_reason(
    *,
    market: str,
    valued: dict[str, Any],
    projected: dict[str, float],
    matchup_ratio: float,
    opportunity_share: float,
    opponent: str,
    vs_opp_hit_rate: float,
    vs_opp_games: int,
    is_home: bool,
    sample_size: int,
) -> str:
    parts = [
        f"Implied {format_implied_odds(valued['implied_odds'])}",
        f"Zone {valued['zone'].upper()}",
        f"Edge {valued['edge']:+.2f}",
        f"Hit% {valued['hit_rate']:.0%}",
        f"Proj {projected['projection']:.1f}",
        f"Baseline {projected['season_avg']:.1f}",
        f"L5 {projected['l5_avg']:.1f}",
        f"L10 {projected['l10_avg']:.1f}",
        f"Snap share {opportunity_share:.0%}",
        f"{market} matchup {matchup_ratio:.2f}x",
        f"Sample {sample_size}",
    ]
    if vs_opp_games:
        parts.append(f"H2H {vs_opp_hit_rate:.0%} vs {opponent} ({vs_opp_games}g)")
    if is_home:
        parts.append("Home boost")
    if sample_size < 5:
        parts.append("Small sample")
    return " | ".join(parts)


# ------------------------------------------------------------------ moneyline


def build_moneyline_candidates(
    *,
    game: dict[str, Any],
    defense_profiles: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    home, away = game["home_team"], game["away_team"]
    home_profile = defense_profiles.get(home, {})
    away_profile = defense_profiles.get(away, {})

    home_score = moneyline_score(
        recent_win_pct=float(home_profile.get("recent_win_pct", 0.5)),
        season_record=float(game.get("home_record", 0.5)),
        points_allowed=float(home_profile.get("allowed_points", 0.0)),
        opponent_points_allowed=float(away_profile.get("allowed_points", 0.0)),
        is_home=True,
    )
    away_score = moneyline_score(
        recent_win_pct=float(away_profile.get("recent_win_pct", 0.5)),
        season_record=float(game.get("away_record", 0.5)),
        points_allowed=float(away_profile.get("allowed_points", 0.0)),
        opponent_points_allowed=float(home_profile.get("allowed_points", 0.0)),
        is_home=False,
    )

    if home_score >= away_score:
        pick, opponent, score, profile = home, away, home_score, home_profile
    else:
        pick, opponent, score, profile = away, home, away_score, away_profile

    win_pct = float(profile.get("recent_win_pct", 0.5))
    return [
        {
            "player_id": f"{pick.lower()}-moneyline",
            "player_name": pick,
            "player_key": "",
            "position": "TEAM",
            "team": pick,
            "opponent": opponent,
            "game_id": game["game_id"],
            "market": "ML",
            "line": "Moneyline",
            "score": round(score, 2),
            "confidence": max(1, min(99, round(score))),
            "tier": "A" if score >= 70 else "B" if score >= 60 else "C",
            "reason": (
                f"Recent win {win_pct:.0%} | "
                f"Allow {profile.get('allowed_points', 0.0):.1f} PPG | "
                f"Sample {int(profile.get('sample', 0))}"
            ),
            "l10_hit_rate": win_pct,
            "l5_hit_rate": win_pct,
            "spread": game.get("spread") if pick == home else (
                None if game.get("spread") is None else -float(game["spread"])
            ),
            "over_under": game.get("over_under"),
            "indoor": game.get("indoor", False),
        }
    ]


def moneyline_score(
    *,
    recent_win_pct: float,
    season_record: float,
    points_allowed: float,
    opponent_points_allowed: float,
    is_home: bool,
) -> float:
    # Defensive edge is measured head-to-head — this defense's allowance
    # against the other defense's — and centered so an even matchup
    # contributes 0.5 rather than an arbitrary constant.
    defense_component = min(max(((opponent_points_allowed - points_allowed) + 10.0) / 20.0, 0.0), 1.0)
    home_component = 0.06 if is_home else 0.0
    return 100 * (
        (recent_win_pct * 0.42)
        + (season_record * 0.28)
        + (defense_component * 0.24)
        + home_component
    )
