from __future__ import annotations

from collections import defaultdict
import json

from app.builders.nba_research_board import build_nba_research_board
from app.builders.universal_game_builder import empty_markets_for
from app.collectors.nba_collector import NBA_MARKETS, collect_nba_raw_data
from app.outputs.json_writer import write_json
from app.utils.dates import timestamp_et


def build_nba_board(*, config, paths) -> dict:
    raw_payload = collect_nba_raw_data(paths.data_raw)
    previous_pick = load_previous_pick(paths)
    games_output = []
    pinned_candidates = []
    processed_games = []
    all_candidates = []

    for raw_game in raw_payload["games"]:
        candidates = [candidate for candidate in raw_game["candidates"] if candidate["score"] > 0]
        candidates = apply_anti_correlation(candidates)
        processed_games.append({"raw": raw_game, "candidates": candidates})
        all_candidates.extend(candidates)

    pick_of_day = build_pick_of_day(processed_games, previous_pick, sport="NBA")
    game_clusters = build_game_clusters(processed_games)
    section_boards = build_section_boards(all_candidates, config.top_market_limit)
    hero_pick = build_hero_pick(pick_of_day, all_candidates, sport="NBA")

    write_json(
        paths.data_processed / "nba_processed.json",
        {
            "sport": "NBA",
            "date": raw_payload["date"],
            "season_type": raw_payload.get("season_type"),
            "pick_of_day": pick_of_day,
            "games": processed_games,
        },
    )

    for processed_game in processed_games:
        raw_game = processed_game["raw"]
        candidates = sorted(processed_game["candidates"], key=lambda row: (row["score"], row["confidence"]), reverse=True)
        market_bucket = defaultdict(list)
        for candidate in candidates:
            market_bucket[candidate["market"]].append(to_board_row(candidate))

        top_signals = build_market_diverse_top_signals(
            candidates=candidates,
            limit=config.top_signals_per_game,
        )

        games_output.append(
            {
                "game_id": raw_game["game_id"],
                "matchup": f'{raw_game["away_team"]} @ {raw_game["home_team"]}',
                "time": raw_game["time"],
                "top_signals": top_signals,
                "markets": {
                    **empty_markets_for(NBA_MARKETS),
                    **{market: rows[: config.top_market_limit] for market, rows in market_bucket.items()},
                },
            }
        )
        pinned_candidates.extend(candidate for candidate in candidates if candidate["market"] == "PTS")

    pinned_players = [to_board_row(candidate) for candidate in sorted(pinned_candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True)[:10]]
    consistency_players = build_consistency_board(all_candidates, config.top_market_limit)
    return {
        "sport": "NBA",
        "date": raw_payload["date"],
        "last_updated": timestamp_et(),
        "hero_pick": hero_pick,
        "game_clusters": game_clusters,
        "section_boards": section_boards,
        "pinned_board": {
            "title": "PTS Top 10",
            "market": "PTS",
            "players": pinned_players,
        },
        "consistency_board": {
            "title": "Consistency Top 10",
            "market": "MIX",
            "players": consistency_players,
        },
        "research_board": build_nba_research_board(
            candidates=all_candidates,
            config=config,
            paths=paths,
        ),
        "games": games_output,
    }


def to_board_row(candidate: dict) -> dict:
    row = {
        "player_id": str(candidate["player_id"]),
        "player_name": candidate["player_name"],
        "team": candidate["team"],
        "opponent": candidate["opponent"],
        "line": candidate["line"],
        "score": round(float(candidate["score"]), 2),
        "confidence": int(candidate["confidence"]),
        "tier": candidate["tier"],
        "reason": candidate["reason"],
    }
    if "implied_odds" in candidate:
        row["implied_odds"] = candidate["implied_odds"]
    if "value_zone" in candidate:
        row["value_zone"] = candidate["value_zone"]
    if "edge" in candidate:
        row["edge"] = candidate["edge"]
    if "model_hit_rate" in candidate:
        row["model_hit_rate"] = candidate["model_hit_rate"]
    return row


def apply_anti_correlation(candidates: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for candidate in candidates:
        if candidate["market"] == "ML":
            continue
        grouped[(candidate["game_id"], candidate["team"], candidate["market"])].append(candidate)

    for group_candidates in grouped.values():
        unique_players = {}
        for candidate in group_candidates:
            unique_players.setdefault(candidate["player_name"], candidate)
        ranked = sorted(unique_players.values(), key=lambda row: row["score"], reverse=True)
        if len(ranked) < 2:
            continue
        leader = ranked[0]["player_name"]
        for follower in ranked[1:]:
            follower["reason"] = f"{follower['reason']} | Anti-correlation: same team as {leader}"
            follower["score"] = round(max(follower["score"] - 1.25, 0.0), 2)
            follower["confidence"] = max(1, min(99, round(follower["score"])))
    return candidates


def build_market_diverse_top_signals(*, candidates: list[dict], limit: int) -> list[dict]:
    preferred_markets = ("AST", "REB", "PTS", "3PM", "ML")
    selected: list[dict] = []
    used_players: set[str] = set()

    for market in preferred_markets:
        market_candidate = next(
            (
                candidate for candidate in candidates
                if candidate["market"] == market and candidate["player_name"] not in used_players
            ),
            None,
        )
        if market_candidate is None:
            continue
        selected.append(market_candidate)
        used_players.add(market_candidate["player_name"])
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
            "score": candidate["score"],
            "confidence": candidate["confidence"],
            "tier": candidate["tier"],
        }
        for candidate in selected
    ]


def parse_line_floor(line: str) -> float:
    if not line:
        return 0.0
    token = str(line).split()[0]
    token = token.replace("+", "").replace("-", "")
    try:
        return float(token)
    except ValueError:
        return 0.0


def featured_prop_floor(candidate: dict, sport: str) -> float:
    market = candidate.get("market")
    floors = {
        "NBA": {"PTS": 15.0, "AST": 5.0, "REB": 6.0, "3PM": 2.0},
        "WNBA": {"PTS": 12.0, "AST": 4.0, "REB": 5.0, "3PM": 2.0},
    }
    return floors.get(sport.upper(), floors["NBA"]).get(str(market), 0.0)


VALUE_ZONE_FEATURED = {"aim", "value", "lean", "longshot"}


def is_featured_prop(candidate: dict, sport: str) -> bool:
    market = candidate.get("market")
    if market == "ML":
        return False
    if candidate.get("tier") not in {"A", "B"}:
        return False
    # Value-pricing path: the natural line by design lands near a 0.50 hit rate,
    # so the legacy 0.65 cutoff would reject every candidate. Trust the
    # value-zone classification instead.
    zone = candidate.get("value_zone")
    if zone is not None:
        if zone not in VALUE_ZONE_FEATURED:
            return False
    elif float(candidate.get("l5_hit_rate", 0.0) or 0.0) < 0.65:
        return False
    floor = featured_prop_floor(candidate, sport)
    value = parse_line_floor(str(candidate.get("line", "")))
    return value >= floor if floor else True


VALUE_ZONE_RANK_BONUS = {"aim": 3.0, "value": 2.5, "longshot": 1.5, "lean": 1.0}


def featured_prop_score(candidate: dict, sport: str) -> float:
    market_bonus = {"PTS": 1.5, "AST": 2.0, "REB": 1.75, "3PM": 1.0}
    tier_bonus = {"A": 2.5, "B": 1.0}
    floor = featured_prop_floor(candidate, sport)
    value = parse_line_floor(str(candidate.get("line", "")))
    floor_margin = max(0.0, value - floor) * 0.8
    h2h_bonus = 0.75 if "H2H" in str(candidate.get("reason", "")) else 0.0
    zone_bonus = VALUE_ZONE_RANK_BONUS.get(str(candidate.get("value_zone", "")), 0.0)
    return round(
        float(candidate.get("score", 0.0))
        + market_bonus.get(str(candidate.get("market")), 0.0)
        + tier_bonus.get(str(candidate.get("tier")), 0.0)
        + floor_margin
        + h2h_bonus
        + zone_bonus,
        2,
    )


def build_pick_of_day(processed_games: list[dict], previous_pick: dict | None, *, sport: str = "NBA") -> dict | None:
    qualified = []
    for processed_game in processed_games:
        for candidate in processed_game["candidates"]:
            if is_featured_prop(candidate, sport):
                qualified.append(candidate)
    if not qualified:
        return previous_pick
    ranked = sorted(
        qualified,
        key=lambda row: (
            featured_prop_score(row, sport),
            row["score"],
            row.get("l5_hit_rate", 0.0),
            row.get("l10_hit_rate", 0.0),
        ),
        reverse=True,
    )
    best = ranked[0]
    return _summarize_pick(best)


def build_hero_pick(pick_of_day: dict | None, candidates: list[dict], *, sport: str = "NBA") -> dict | None:
    if pick_of_day:
        return {
            **pick_of_day,
            "label": "Pick of the Day",
        }
    if not candidates:
        return None
    featured = [candidate for candidate in candidates if is_featured_prop(candidate, sport)]
    pool = featured or [candidate for candidate in candidates if candidate.get("market") != "ML"] or candidates
    best = sorted(
        pool,
        key=lambda row: (
            featured_prop_score(row, sport),
            row["score"],
            row["confidence"],
        ),
        reverse=True,
    )[0]
    return {
        **_summarize_pick(best),
        "label": "Signal Leader",
    }


def _summarize_pick(candidate: dict) -> dict:
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
    }
    for key in ("implied_odds", "value_zone", "edge", "model_hit_rate"):
        if key in candidate:
            summary[key] = candidate[key]
    return summary


def build_game_clusters(processed_games: list[dict]) -> list[dict]:
    clusters = []
    for processed_game in processed_games:
        candidates = [candidate for candidate in processed_game["candidates"] if candidate["market"] != "ML"]
        ranked = sorted(candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True)
        if not ranked:
            continue
        top_candidates = ranked[:3]
        cluster_score = round(sum(candidate["score"] for candidate in top_candidates) / len(top_candidates), 2)
        clusters.append(
            {
                "game_id": processed_game["raw"]["game_id"],
                "matchup": f'{processed_game["raw"]["away_team"]} @ {processed_game["raw"]["home_team"]}',
                "top_score": cluster_score,
                "signals": [
                    {
                        "player_name": candidate["player_name"],
                        "market": candidate["market"],
                        "line": candidate["line"],
                        "score": round(float(candidate["score"]), 2),
                        "tier": candidate["tier"],
                    }
                    for candidate in top_candidates
                ],
            }
        )
    return sorted(clusters, key=lambda row: row["top_score"], reverse=True)[:3]


def build_section_boards(candidates: list[dict], limit: int) -> dict[str, list[dict]]:
    section_map = {
        "PTS": "Scoring Board",
        "AST": "Playmaker Assists",
        "REB": "Glass / Rebounds",
        "3PM": "3PT Heat",
    }
    boards = {}
    for market, title in section_map.items():
        market_candidates = [candidate for candidate in candidates if candidate["market"] == market]
        ranked = sorted(market_candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True)
        boards[market] = {
            "title": title,
            "market": market,
            "players": [to_board_row(candidate) for candidate in ranked[:limit]],
        }

    ladder_candidates = []
    for market in ("AST", "REB"):
        market_candidates = [candidate for candidate in candidates if candidate["market"] == market]
        ranked = sorted(market_candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True)
        for candidate in ranked[:5]:
            ladder_candidates.append(
                {
                    "player_id": str(candidate["player_id"]),
                    "player_name": candidate["player_name"],
                    "team": candidate["team"],
                    "opponent": candidate["opponent"],
                    "line": candidate["line"],
                    "score": round(float(candidate["score"]), 2),
                    "confidence": int(candidate["confidence"]),
                    "tier": candidate["tier"],
                    "reason": candidate["reason"],
                    "market": candidate["market"],
                    "ladder": build_ladder_steps(candidate["line"]),
                }
            )

    boards["LADDERS"] = {
        "title": "Ladder Sleepers",
        "market": "LADDERS",
        "players": sorted(ladder_candidates, key=lambda row: (row["score"], row["confidence"]), reverse=True)[:5],
    }
    return boards


def build_consistency_board(candidates: list[dict], limit: int) -> list[dict]:
    rows = []
    for candidate in candidates:
        if candidate["market"] not in {"PTS", "REB", "AST"}:
            continue
        if candidate["tier"] == "C":
            continue
        row = to_board_row(candidate)
        row["score"] = round(consistency_score(candidate), 2)
        row["confidence"] = max(1, min(99, round(row["score"])))
        rows.append(row)

    rows.sort(key=lambda row: (row["score"], row["confidence"]), reverse=True)
    return rows[:limit]


def consistency_score(candidate: dict) -> float:
    market_bonus = {
        "AST": 3.4,
        "REB": 2.8,
        "PTS": 2.0,
    }.get(candidate["market"], 0.0)
    hit_rate_bonus = (candidate.get("l10_hit_rate", 0.0) * 4.0) + (candidate.get("l5_hit_rate", 0.0) * 3.0)
    minute_bonus = min(candidate.get("minutes_projection", 0.0), 40.0) / 10.0
    return float(candidate["score"]) + market_bonus + hit_rate_bonus + minute_bonus


def build_ladder_steps(line: str) -> list[str]:
    base = extract_line_value(line)
    if base <= 0:
        return []
    return [f"{base + step}+" for step in (0, 2, 4)]


def extract_line_value(line: str) -> int:
    try:
        return int(str(line).split("+", maxsplit=1)[0])
    except (TypeError, ValueError):
        return 0


def load_previous_pick(paths) -> dict | None:
    previous_path = paths.data_processed / "nba_processed.json"
    if not previous_path.exists():
        return None
    try:
        payload = json.loads(previous_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload.get("pick_of_day")
