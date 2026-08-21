"""First-touchdown-scorer board for the NFL: a Poisson race over the slate.

The anytime-TD market already on this board answers "does he score at all?"
This module answers a different question -- "who scores the game's FIRST
touchdown, and who scores his own team's first?" -- which is the shape of
the first-basket / first-team-basket pair the NBA Opening Edge section
models on the basketball side (model/opening-edge). Same family of market,
same honesty rules: model-fair prices, no sportsbook feed, report only.

THE MODEL
---------
Every rostered candidate the collector scored already carries `td_lambda`,
the matchup-adjusted expected number of touchdowns that player scores in
this game (nfl_model.project_td_lambda -- rushing + receiving, shrunk
toward a position prior). Treat each player's scoring as an independent
Poisson process running through the game with intensity proportional to
that lambda. For a set of independent Poisson processes, the probability
that process i produces the FIRST event is exactly its share of the total
intensity -- the arrival order is independent of how long the wait was:

    P(player i scores first | at least one TD) = lambda_i / LAMBDA_total

and the game can also end with nobody scoring a touchdown at all:

    P(no TD in the game) = exp(-LAMBDA_total)
    P(player i scores the game's first TD) = lambda_i / LAMBDA_total
                                             * (1 - exp(-LAMBDA_total))

The same math run over one team's players only (LAMBDA_team) gives the
"first team TD scorer" market, where the two teams' openings are graded
separately.

WHAT THIS DELIBERATELY DOES NOT CLAIM
-------------------------------------
1. Proportional-intensity is an assumption, not a measurement. A goal-line
   back is likelier to score EARLY (short-field, script-independent) than a
   deep threat with the same season TD rate, and this model cannot see that
   -- the collector fetches season TD totals, not red-zone or drive-position
   splits. Every row is scored on its whole-game rate.
2. The modeled pool is not every possible scorer. The collector drops
   players under MINIMUM_GAMES_PLAYED (rookies, backups with no 2025
   sample) and never models defensive or special-teams returns at all.
   Charging the modeled players for 100% of the first-TD market would
   overstate every one of them, so a residual bucket carries that mass
   explicitly -- see UNMODELED_TD_SHARE.
3. These are model-fair prices for ranking, not calibrated probabilities,
   and first-TD markets are high variance. Nothing here places a wager.

Pure math + a plain-dict candidate interface -- no ESPN I/O -- independently
unit-testable against fixed synthetic inputs, same convention as
nfl_qb_stack.py / nfl_rb_stack.py.
"""

from __future__ import annotations

import math
from typing import Any

from app.scoring.value import format_implied_odds, hit_rate_to_implied_odds

# Share of the first-TD market that belongs to scorers this pipeline does
# not model: defensive and special-teams returns, plus offensive players the
# collector filtered out for having no usable 2025 sample (undrafted
# rookies, camp-body backups, a second QB sneaking one in). Applied as extra
# race intensity split evenly between the two teams, so it lowers every
# modeled player's first-TD probability rather than silently inflating them
# to sum to 1. Anchored on the long-running NFL split where roughly one
# touchdown in ten is scored by a defender, a returner, or a player off the
# fringe of the depth chart -- a documented prior in the same spirit as
# nfl_model.POSITION_PRIORS, not a fitted constant, and surfaced on the
# board as `unmodeled_share_pct` so the haircut is visible.
UNMODELED_TD_SHARE = 0.10

# A player needs a real share of the race to earn a row. Below this the fair
# price is a four-figure longshot built on a rounding artifact of a shrunk
# season rate, which is noise dressed as a signal.
MIN_FIRST_TD_PROBABILITY = 0.015

# Board sizes.
PLAYERS_PER_GAME = 8
TOP_BOARD_LIMIT = 12
TEAM_BOARD_LIMIT = 12
PARLAY_LIMIT = 4
PARLAY_LEG_POOL = 8


def collect_scorer_lambdas(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per player from a game's candidate rows.

    The collector tags `td_lambda` onto every row a player produces (his TD
    rungs, his yardage rows, all of them), so the same player arrives here
    many times over. Deduplicate on player_id and keep the first row's
    identity fields; the lambda is identical across that player's rows by
    construction.
    """
    scorers: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        player_id = str(candidate.get("player_id") or "")
        lam = candidate.get("td_lambda")
        if not player_id or lam is None or player_id in scorers:
            continue
        try:
            lam = float(lam)
        except (TypeError, ValueError):
            continue
        if lam <= 0:
            continue
        scorers[player_id] = {
            "player_id": player_id,
            "player_name": candidate.get("player_name", player_id),
            "team": candidate.get("team", ""),
            "opponent": candidate.get("opponent", ""),
            "position": candidate.get("position", ""),
            "td_lambda": lam,
        }
    return list(scorers.values())


def unmodeled_lambda(modeled_lambda: float, share: float = UNMODELED_TD_SHARE) -> float:
    """Race intensity to hand to scorers outside the modeled pool.

    `share` is the fraction of ALL touchdowns those scorers account for, so
    the extra intensity solves share = other / (modeled + other):

        other = modeled * share / (1 - share)
    """
    share = min(max(0.0, share), 0.95)
    return max(0.0, modeled_lambda) * share / (1.0 - share)


def first_event_probability(player_lambda: float, total_lambda: float) -> float:
    """P(this player's process produces the game's first touchdown).

    Share of total race intensity, scaled by the chance the race produces
    any event at all. Returns 0.0 for an empty race rather than dividing by
    zero (an all-zero slate is a data outage, not a 100% anybody).
    """
    if total_lambda <= 0 or player_lambda <= 0:
        return 0.0
    return (player_lambda / total_lambda) * (1.0 - math.exp(-total_lambda))


def _priced(probability: float) -> dict[str, Any]:
    return {
        "prob_pct": round(probability * 100, 2),
        "fair_odds": format_implied_odds(hit_rate_to_implied_odds(probability)),
    }


def build_first_td_for_game(
    *,
    game_id: str,
    matchup: str,
    time: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """First-TD race for one game, or None when nothing is modelable.

    Returns both markets for every qualifying player -- the game-wide first
    TD and his own team's first TD -- plus the two team-level rows (which
    team opens the scoring) and the game's no-touchdown probability.
    """
    scorers = collect_scorer_lambdas(candidates)
    if not scorers:
        return None

    teams = sorted({scorer["team"] for scorer in scorers if scorer["team"]})
    if not teams:
        return None

    team_lambda = {
        team: sum(scorer["td_lambda"] for scorer in scorers if scorer["team"] == team)
        for team in teams
    }
    modeled_lambda = sum(team_lambda.values())
    if modeled_lambda <= 0:
        return None

    # The residual splits evenly between the two teams: a defensive or
    # special-teams score is a coin flip on which sideline it comes from,
    # and there is no collected signal that would justify tilting it.
    other_total = unmodeled_lambda(modeled_lambda)
    other_per_team = other_total / len(teams)
    total_lambda = modeled_lambda + other_total

    players: list[dict[str, Any]] = []
    for scorer in scorers:
        team = scorer["team"]
        game_first = first_event_probability(scorer["td_lambda"], total_lambda)
        if game_first < MIN_FIRST_TD_PROBABILITY:
            continue
        team_race = team_lambda.get(team, 0.0) + other_per_team
        team_first = first_event_probability(scorer["td_lambda"], team_race)
        share_of_team = scorer["td_lambda"] / team_race if team_race > 0 else 0.0
        game_priced = _priced(game_first)
        team_priced = _priced(team_first)
        players.append(
            {
                "player_id": scorer["player_id"],
                "player_name": scorer["player_name"],
                "team": team,
                "opponent": scorer["opponent"],
                "position": scorer["position"],
                "game_id": game_id,
                "matchup": matchup,
                "td_lambda": round(scorer["td_lambda"], 3),
                "first_td_prob_pct": game_priced["prob_pct"],
                "first_td_fair_odds": game_priced["fair_odds"],
                "team_first_td_prob_pct": team_priced["prob_pct"],
                "team_first_td_fair_odds": team_priced["fair_odds"],
                "share_of_team_pct": round(share_of_team * 100, 1),
                "reason": (
                    f"TD λ {scorer['td_lambda']:.2f} of {total_lambda:.2f} in the game "
                    f"({team} λ {team_lambda.get(team, 0.0):.2f}) | "
                    f"{share_of_team * 100:.0f}% of {team}'s first-TD race | "
                    "whole-game TD rate, no red-zone split collected"
                ),
            }
        )

    if not players:
        return None
    players.sort(key=lambda row: row["first_td_prob_pct"], reverse=True)

    team_rows = []
    for team in teams:
        opens = first_event_probability(team_lambda[team] + other_per_team, total_lambda)
        priced = _priced(opens)
        leaders = [row for row in players if row["team"] == team]
        team_rows.append(
            {
                "team": team,
                "opponent": next((row["opponent"] for row in players if row["team"] == team), ""),
                "game_id": game_id,
                "matchup": matchup,
                "td_lambda": round(team_lambda[team], 3),
                "opens_scoring_prob_pct": priced["prob_pct"],
                "opens_scoring_fair_odds": priced["fair_odds"],
                "top_scorer": leaders[0]["player_name"] if leaders else None,
                "top_scorer_prob_pct": leaders[0]["team_first_td_prob_pct"] if leaders else None,
            }
        )
    team_rows.sort(key=lambda row: row["opens_scoring_prob_pct"], reverse=True)

    return {
        "game_id": game_id,
        "matchup": matchup,
        "time": time,
        "modeled_lambda": round(modeled_lambda, 3),
        "total_lambda": round(total_lambda, 3),
        "unmodeled_share_pct": round(UNMODELED_TD_SHARE * 100, 1),
        "no_td_prob_pct": round(math.exp(-total_lambda) * 100, 2),
        "teams": team_rows,
        "players": players[:PLAYERS_PER_GAME],
    }


def build_first_td_parlays(games: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Cross-game first-TD combos, one leg per game.

    One leg per game on purpose: two first-TD legs inside the SAME game are
    mutually exclusive on the game-wide market (only one player scores the
    first touchdown), so a same-game pair is not a long shot, it is a dead
    ticket. Across games the legs are genuinely independent -- separate
    stadiums, separate openings -- so the joint probability is the product,
    with no correlation term to model. This is the honest version of the
    3-leg cross-game first-basket ticket these markets are usually played
    as.
    """
    legs = []
    for game in games:
        if game["players"]:
            best = game["players"][0]
            legs.append(
                {
                    "player_id": best["player_id"],
                    "player_name": best["player_name"],
                    "team": best["team"],
                    "matchup": best["matchup"],
                    "prob": best["first_td_prob_pct"] / 100,
                    "fair_odds": best["first_td_fair_odds"],
                }
            )
    legs.sort(key=lambda leg: leg["prob"], reverse=True)
    pool = legs[:PARLAY_LEG_POOL]

    parlays: dict[str, list[dict[str, Any]]] = {}
    for leg_count in (2, 3):
        tickets = []
        for start in range(0, max(0, len(pool) - leg_count + 1)):
            combo = pool[start: start + leg_count]
            if len(combo) < leg_count:
                continue
            joint = 1.0
            for leg in combo:
                joint *= leg["prob"]
            if joint <= 0:
                continue
            tickets.append(
                {
                    "legs": [
                        {
                            "player_id": leg["player_id"],
                            "player_name": leg["player_name"],
                            "team": leg["team"],
                            "matchup": leg["matchup"],
                            "prob_pct": round(leg["prob"] * 100, 2),
                            "fair_odds": leg["fair_odds"],
                        }
                        for leg in combo
                    ],
                    "joint_prob_pct": round(joint * 100, 3),
                    "fair_odds": format_implied_odds(hit_rate_to_implied_odds(joint)),
                    "decimal": round(1 / joint, 1),
                }
            )
        tickets.sort(key=lambda ticket: ticket["joint_prob_pct"], reverse=True)
        parlays[f"{leg_count}_leg"] = tickets[:PARLAY_LIMIT]
    return parlays


def build_first_td_board(games: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the slate-wide first-TD board from per-game races.

    Always returns a populated shape -- empty lists rather than an absent
    field -- so the frontend can rely on the key existing off-season, the
    same "always present, empty rather than fabricated" convention the rest
    of the NFL board follows.
    """
    ranked_games = [game for game in games if game]
    top_board = sorted(
        (player for game in ranked_games for player in game["players"]),
        key=lambda row: row["first_td_prob_pct"],
        reverse=True,
    )[:TOP_BOARD_LIMIT]
    team_board = sorted(
        (team for game in ranked_games for team in game["teams"]),
        key=lambda row: row["opens_scoring_prob_pct"],
        reverse=True,
    )[:TEAM_BOARD_LIMIT]
    return {
        "title": "First TD Scorer",
        "market": "FirstTD",
        "method": (
            "Poisson race: each player's matchup-adjusted anytime-TD lambda is his share "
            "of the game's total scoring intensity, so P(first TD) = lambda / total * "
            "P(the game produces a touchdown at all)."
        ),
        "assumptions": [
            "Scoring intensity is proportional to the whole-game TD rate — no red-zone or "
            "drive-position split is collected, so a goal-line back and a deep threat with "
            "the same season rate are treated alike.",
            f"{round(UNMODELED_TD_SHARE * 100)}% of the first-TD race is held back for scorers this "
            "pipeline does not model (defensive and special-teams returns, players with no "
            "usable prior-season sample), split evenly between the two teams.",
            "The race total is the sum of every modeled player's anytime-TD lambda, which "
            "runs hotter than a real game's touchdown total. First-TD shares are a ratio, so "
            "that mostly cancels — but it makes the no-touchdown figure optimistic, and it is "
            "shown per game rather than folded silently into the prices.",
            "Model-fair prices, no sportsbook feed. First-TD markets are high variance — "
            "research only.",
        ],
        "unmodeled_share_pct": round(UNMODELED_TD_SHARE * 100, 1),
        "top_board": top_board,
        "team_board": team_board,
        "games": ranked_games,
        "parlays": build_first_td_parlays(ranked_games),
    }
