"""NFL board — two categories on one export.

1. **Pure predictions** — model probabilities and quoted lines for the slate,
   built from game logs and opponent allowances alone.
2. **Salary categories (historical)** — what each contract-money tier has
   produced over a three-season rolling window, and where the slate's paid
   players sit against their own tier.

The categories are deliberately one-directional: the salary board reads the
prediction candidates (to know who is playing), but nothing in the salary layer
feeds back into a prediction score. Contract money is a *description* of a
player, not evidence about tonight's game, and wiring it into the model would
quietly turn "who is expensive" into "who is good".
"""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from app.builders.nfl_salary_board import build_nfl_salary_board
from app.builders.universal_game_builder import empty_markets_for
from app.collectors.nfl_collector import HISTORY_SEASONS, NFL_MARKETS, collect_nfl_raw_data
from app.collectors.nfl_contracts import load_contracts
from app.models.nfl_model import build_nfl_candidates
from app.outputs.json_writer import write_json
from app.sim.edge import build_sim_board
from app.sim.sim_engine import sim_prob_pct, simulate_candidates
from app.utils.dates import timestamp_et


SECTION_TITLES = {
    "PASS_YDS": "Passing Board",
    "RUSH_YDS": "Ground Game",
    "REC_YDS": "Receiving Yards",
    "REC": "Reception Volume",
    "TD": "Touchdown Board",
}

# Markets where teammates split a finite pool (targets, carries, goal-line
# looks). Stacking two of them from one offense is a correlated bet, so the
# lower-ranked teammate gets pulled down rather than double-counted.
CONTESTED_MARKETS = {"RUSH_YDS", "REC_YDS", "REC", "TD"}

CATEGORIES = [
    {
        "key": "pure_predictions",
        "title": "Pure Predictions",
        "description": (
            "Model-derived lines and probabilities for the slate. Built from game logs, "
            "opponent allowances, and game-script simulation. No salary input."
        ),
    },
    {
        "key": "salary_historical",
        "title": "Salary Categories (Historical)",
        "description": (
            "Contract-money tiers scored on what they have actually produced over a "
            "three-season rolling window. Descriptive, not predictive."
        ),
    },
]


def build_nfl_board(*, config, paths) -> dict:
    # Contracts first: the collector only pulls the deep history window for
    # players the salary board can actually place, so the money file defines
    # how much history gets fetched.
    contracts = load_contracts(paths)
    history_keys = set(contracts.get("players", {}).keys())

    raw_payload = collect_nfl_raw_data(
        paths.data_raw,
        history_keys=history_keys,
        history_seasons=HISTORY_SEASONS,
    )

    candidates = [candidate for candidate in build_nfl_candidates(raw_payload) if candidate["score"] > 0]
    candidates = apply_anti_correlation(candidates)
    simulate_candidates(candidates, sport="NFL")

    previous_pick = load_previous_pick(paths)
    pick_of_day = build_pick_of_day(candidates, previous_pick)
    candidates_by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_game[candidate["game_id"]].append(candidate)

    write_json(
        paths.data_processed / "nfl_processed.json",
        {
            "sport": "NFL",
            "date": raw_payload["date"],
            "week": raw_payload.get("week"),
            "season_type": raw_payload.get("season_type"),
            "pick_of_day": pick_of_day,
            "candidate_count": len(candidates),
        },
    )

    games_output = [
        build_game_output(game=game, candidates=candidates_by_game.get(game["game_id"], []), config=config)
        for game in raw_payload.get("games", [])
    ]

    return {
        "sport": "NFL",
        "date": raw_payload["date"],
        "last_updated": timestamp_et(),
        "week": raw_payload.get("week"),
        "season_type": raw_payload.get("season_type"),
        "categories": CATEGORIES,
        "hero_pick": build_hero_pick(pick_of_day, candidates),
        "game_clusters": build_game_clusters(raw_payload.get("games", []), candidates_by_game),
        "section_boards": build_section_boards(candidates, config.top_market_limit),
        "pinned_board": {
            "title": "Touchdown Top 10",
            "market": "TD",
            "players": top_rows(candidates, market="TD", limit=10),
        },
        "best_available_board": {
            "title": "Best Available",
            "subtitle": "A-tier across every market",
            "players": build_best_available(candidates),
        },
        "salary_board": build_nfl_salary_board(
            history_profiles=raw_payload.get("history_profiles", {}),
            contracts=contracts,
            candidates=candidates,
            history_seasons=raw_payload.get("history_seasons", []),
        ),
        "sim_board": build_sim_board(candidates, sport="NFL"),
        "games": games_output,
    }


# ---------------------------------------------------------------- board rows


def to_board_row(candidate: dict[str, Any]) -> dict[str, Any]:
    row = {
        "player_id": str(candidate["player_id"]),
        "player_name": candidate["player_name"],
        "team": candidate["team"],
        "opponent": candidate["opponent"],
        "market": candidate.get("market", ""),
        "line": candidate["line"],
        "score": round(float(candidate["score"]), 2),
        "confidence": int(candidate["confidence"]),
        "tier": candidate["tier"],
        "reason": candidate["reason"],
        "sim_prob_pct": sim_prob_pct(candidate),
    }
    for key in (
        "position",
        "implied_odds",
        "value_zone",
        "edge",
        "model_hit_rate",
        "projection",
        "matchup_ratio",
        "opportunity_share",
        "sample_size",
        "lineup_status",
        "team_star_outs",
        "team_star_gtd",
        "team_usage_boost",
        "team_lost_usage",
    ):
        if key in candidate:
            row[key] = candidate[key]
    ladder = candidate.get("ladder")
    if ladder:
        row["ladder"] = {int(threshold): round(float(prob), 4) for threshold, prob in sorted(ladder.items())}
    return row


def top_rows(candidates: list[dict[str, Any]], *, market: str, limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        (candidate for candidate in candidates if candidate.get("market") == market),
        key=lambda row: (row["score"], row["confidence"]),
        reverse=True,
    )
    return [to_board_row(candidate) for candidate in ranked[:limit]]


def build_best_available(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    pool: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True):
        if candidate.get("tier") != "A" or candidate.get("market") == "ML":
            continue
        key = (str(candidate["player_id"]), candidate["market"])
        if key in seen:
            continue
        seen.add(key)
        pool.append(candidate)
        if len(pool) >= 10:
            break
    return [to_board_row(candidate) for candidate in pool]


def build_section_boards(candidates: list[dict[str, Any]], limit: int) -> dict[str, dict[str, Any]]:
    boards: dict[str, dict[str, Any]] = {}
    for market, title in SECTION_TITLES.items():
        boards[market] = {
            "title": title,
            "market": market,
            "players": top_rows(candidates, market=market, limit=limit),
        }
    return boards


def build_game_output(*, game: dict[str, Any], candidates: list[dict[str, Any]], config) -> dict[str, Any]:
    ranked = sorted(candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True)
    market_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in ranked:
        market_bucket[candidate["market"]].append(to_board_row(candidate))

    return {
        "game_id": game["game_id"],
        "matchup": f'{game["away_team"]} @ {game["home_team"]}',
        "time": game.get("time", "TBD"),
        "top_signals": build_market_diverse_top_signals(
            candidates=ranked,
            limit=config.top_signals_per_game,
        ),
        "markets": {
            **empty_markets_for(NFL_MARKETS),
            **{market: rows[: config.top_market_limit] for market, rows in market_bucket.items()},
        },
    }


def build_market_diverse_top_signals(*, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """One signal per market before doubling up, and never the same player twice."""
    preferred = ("TD", "REC_YDS", "RUSH_YDS", "PASS_YDS", "REC", "ML")
    selected: list[dict[str, Any]] = []
    used_players: set[str] = set()

    for market in preferred:
        match = next(
            (
                candidate for candidate in candidates
                if candidate["market"] == market and candidate["player_name"] not in used_players
            ),
            None,
        )
        if match is None:
            continue
        selected.append(match)
        used_players.add(match["player_name"])
        if len(selected) == limit:
            break

    if len(selected) < limit:
        for candidate in candidates:
            if candidate["player_name"] in used_players:
                continue
            selected.append(candidate)
            used_players.add(candidate["player_name"])
            if len(selected) == limit:
                break

    return [
        {
            "market": candidate["market"],
            "player_name": candidate["player_name"],
            "line": candidate["line"],
            "score": round(float(candidate["score"]), 2),
            "confidence": int(candidate["confidence"]),
            "tier": candidate["tier"],
            "sim_prob_pct": sim_prob_pct(candidate),
        }
        for candidate in selected
    ]


def build_game_clusters(games: list[dict[str, Any]], candidates_by_game: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    clusters = []
    for game in games:
        ranked = sorted(
            (row for row in candidates_by_game.get(game["game_id"], []) if row["market"] != "ML"),
            key=lambda row: (row["score"], row["confidence"]),
            reverse=True,
        )
        if not ranked:
            continue
        top = ranked[:3]
        clusters.append(
            {
                "game_id": game["game_id"],
                "matchup": f'{game["away_team"]} @ {game["home_team"]}',
                "top_score": round(sum(row["score"] for row in top) / len(top), 2),
                "signals": [
                    {
                        "player_name": row["player_name"],
                        "market": row["market"],
                        "line": row["line"],
                        "score": round(float(row["score"]), 2),
                        "tier": row["tier"],
                        "sim_prob_pct": sim_prob_pct(row),
                    }
                    for row in top
                ],
            }
        )
    return sorted(clusters, key=lambda row: row["top_score"], reverse=True)[:3]


# ------------------------------------------------------------------ featured


def apply_anti_correlation(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Damp teammates competing for the same touches inside one market."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        if candidate["market"] not in CONTESTED_MARKETS:
            continue
        grouped[(candidate["game_id"], candidate["team"], candidate["market"])].append(candidate)

    for group in grouped.values():
        ranked = sorted(group, key=lambda row: row["score"], reverse=True)
        if len(ranked) < 2:
            continue
        leader = ranked[0]["player_name"]
        for follower in ranked[1:]:
            follower["reason"] = f"{follower['reason']} | Anti-correlation: shares touches with {leader}"
            follower["score"] = round(max(follower["score"] - 1.25, 1.0), 2)
            follower["confidence"] = max(1, min(99, round(follower["score"])))
    return candidates


def is_featured(candidate: dict[str, Any]) -> bool:
    if candidate.get("market") == "ML":
        return False
    if candidate.get("tier") not in {"A", "B"}:
        return False
    return candidate.get("value_zone") in {"aim", "value", "lean", "longshot"}


FEATURED_MARKET_BONUS = {"TD": 2.0, "REC_YDS": 1.75, "RUSH_YDS": 1.5, "PASS_YDS": 1.25, "REC": 1.0}
FEATURED_TIER_BONUS = {"A": 2.5, "B": 1.0}
FEATURED_ZONE_BONUS = {"aim": 3.0, "value": 2.5, "longshot": 1.5, "lean": 1.0}


def featured_score(candidate: dict[str, Any]) -> float:
    return round(
        float(candidate.get("score", 0.0))
        + FEATURED_MARKET_BONUS.get(str(candidate.get("market")), 0.0)
        + FEATURED_TIER_BONUS.get(str(candidate.get("tier")), 0.0)
        + FEATURED_ZONE_BONUS.get(str(candidate.get("value_zone", "")), 0.0)
        + (0.75 if "H2H" in str(candidate.get("reason", "")) else 0.0),
        2,
    )


def build_pick_of_day(candidates: list[dict[str, Any]], previous_pick: dict[str, Any] | None) -> dict[str, Any] | None:
    qualified = [candidate for candidate in candidates if is_featured(candidate)]
    if not qualified:
        return previous_pick
    best = max(qualified, key=lambda row: (featured_score(row), row["score"], row.get("model_hit_rate", 0.0)))
    return summarize_pick(best)


def build_hero_pick(pick_of_day: dict[str, Any] | None, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if pick_of_day:
        return {**pick_of_day, "label": "Pick of the Week"}
    if not candidates:
        return None
    pool = [candidate for candidate in candidates if candidate.get("market") != "ML"] or candidates
    best = max(pool, key=lambda row: (featured_score(row), row["score"], row["confidence"]))
    return {**summarize_pick(best), "label": "Signal Leader"}


def summarize_pick(candidate: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "player_id": str(candidate["player_id"]),
        "player_name": candidate["player_name"],
        "team": candidate["team"],
        "opponent": candidate["opponent"],
        "market": candidate["market"],
        "line": candidate["line"],
        "score": candidate["score"],
        "confidence": candidate["confidence"],
        "tier": candidate["tier"],
        "reason": candidate["reason"],
        "sim_prob_pct": sim_prob_pct(candidate),
    }
    for key in ("position", "implied_odds", "value_zone", "edge", "model_hit_rate", "lineup_status"):
        if key in candidate:
            summary[key] = candidate[key]
    return summary


def load_previous_pick(paths) -> dict[str, Any] | None:
    previous_path = paths.data_processed / "nfl_processed.json"
    if not previous_path.exists():
        return None
    try:
        payload = json.loads(previous_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload.get("pick_of_day")
