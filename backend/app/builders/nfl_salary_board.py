"""NFL salary categories — what each tier of contract money has historically produced.

Category 2 of the NFL board. The question it answers is not "who will score
tonight" (that is the prediction board's job) but "what does a player at this
salary level actually deliver, and where does today's slate sit against that".

Method:

1. Bucket every contracted player into a league-wide salary category by APY
   (``app.collectors.nfl_contracts.SALARY_TIERS``).
2. Pull each one's game logs across a **three-season rolling window** and
   reduce them to per-game PPR fantasy points — one production currency that
   is comparable between a quarterback and a slot receiver.
3. Reduce each category to a distribution: games, median, quartiles, and the
   production a dollar buys at that tier.
4. Place today's slate against those baselines, so a player is read against
   what his *pay grade* has historically returned rather than against the
   league as a whole.

Every number is computed from the logs; nothing is projected here and nothing
in this module feeds the prediction scoring. If the contracts file is missing,
the board reports ``available: false`` and says why.
"""

from __future__ import annotations

from typing import Any

from app.collectors.nfl_contracts import TIER_LABELS, TIER_ORDER


# A tier summary needs enough games behind it to mean anything. Below this the
# tier is still reported, but flagged thin rather than quietly presented as a
# baseline.
MIN_GAMES_FOR_BASELINE = 25
# Positions are grouped for the per-position cut; anything else is "OTHER".
POSITION_GROUPS = ("QB", "RB", "WR", "TE")


def build_nfl_salary_board(
    *,
    history_profiles: dict[str, dict[str, Any]],
    contracts: dict[str, Any],
    candidates: list[dict[str, Any]],
    history_seasons: list[int] | None = None,
) -> dict[str, Any]:
    contract_rows = contracts.get("players", {})
    meta = contracts.get("meta", {})

    if not contract_rows:
        return {
            "title": "NFL Salary Categories",
            "subtitle": "Historical production by contract salary tier.",
            "available": False,
            "reason": meta.get("reason", "no contracts loaded"),
            "contracts_meta": meta,
            "window": {"seasons": history_seasons or []},
            "tiers": [],
            "slate": [],
        }

    players = build_player_records(history_profiles=history_profiles, contract_rows=contract_rows)
    tier_stats = {tier: summarize_tier(tier, players) for tier in TIER_ORDER}
    slate_rows = build_slate_rows(players=players, candidates=candidates, tier_stats=tier_stats)

    return {
        "title": "NFL Salary Categories",
        "subtitle": "Historical production by contract salary tier (3-season rolling window).",
        "available": True,
        "method": (
            "Players bucketed by contract APY, then reduced to per-game PPR points across the "
            "rolling window. Value index = the player's points per $1M against the median points "
            "per $1M in his own salary tier (1.00 = paying market rate for the production)."
        ),
        "contracts_meta": meta,
        "window": {
            "seasons": history_seasons or sorted({season for record in players for season in record["seasons"]}, reverse=True),
            "players_matched": len(players),
            "contracts_loaded": len(contract_rows),
            "unmatched_contracts": sorted(
                row["player_name"]
                for key, row in contract_rows.items()
                if key not in {record["player_key"] for record in players}
            ),
        },
        "tiers": [tier_stats[tier] for tier in TIER_ORDER],
        "tier_by_position": build_position_breakdown(players),
        "slate": slate_rows,
        "value_leaders": sorted(
            [row for row in slate_rows if row["value_index"] is not None],
            key=lambda row: row["value_index"],
            reverse=True,
        )[:12],
        "below_tier_baseline": sorted(
            [row for row in slate_rows if row["vs_tier_median_pct"] is not None and row["vs_tier_median_pct"] < 0],
            key=lambda row: row["vs_tier_median_pct"],
        )[:12],
    }


# ------------------------------------------------------------------- records


def build_player_records(
    *,
    history_profiles: dict[str, dict[str, Any]],
    contract_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join three-season logs to contract money, one record per player."""
    records: list[dict[str, Any]] = []
    for profile in history_profiles.values():
        key = profile.get("player_key")
        contract = contract_rows.get(key)
        if not contract:
            continue
        logs = profile.get("logs", [])
        if not logs:
            continue

        points = [float(log.get("fantasy_ppr", 0.0)) for log in logs]
        apy_millions = contract["apy"] / 1_000_000.0
        ppg = mean(points)
        records.append(
            {
                "player_key": key,
                "player_name": contract["player_name"],
                "position": normalize_position(contract.get("position") or profile.get("position", "")),
                "team": profile.get("team") or contract.get("team", ""),
                "apy": contract["apy"],
                "apy_millions": round(apy_millions, 2),
                "salary_tier": contract["salary_tier"],
                "estimated_salary": contract["estimated"],
                "games": len(points),
                "seasons": profile.get("seasons", []),
                "ppg": round(ppg, 2),
                "median_ppg": round(median(points), 2),
                "points_per_million": round(ppg / apy_millions, 3) if apy_millions > 0 else None,
                "season_splits": season_splits(logs),
            }
        )
    return records


def season_splits(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-season production inside the window — makes a decline visible."""
    by_season: dict[int, list[float]] = {}
    for log in logs:
        by_season.setdefault(int(log.get("season", 0)), []).append(float(log.get("fantasy_ppr", 0.0)))
    return [
        {"season": season, "games": len(points), "ppg": round(mean(points), 2)}
        for season, points in sorted(by_season.items(), reverse=True)
    ]


def normalize_position(position: str) -> str:
    position = str(position or "").upper()
    if position == "FB":
        return "RB"
    return position if position in POSITION_GROUPS else "OTHER"


# --------------------------------------------------------------------- tiers


def summarize_tier(tier: str, players: list[dict[str, Any]]) -> dict[str, Any]:
    members = [record for record in players if record["salary_tier"] == tier]
    ppgs = [record["ppg"] for record in members]
    per_million = [record["points_per_million"] for record in members if record["points_per_million"] is not None]
    games = sum(record["games"] for record in members)

    return {
        "tier": tier,
        "label": TIER_LABELS.get(tier, tier),
        "players": len(members),
        "games": games,
        "median_ppg": round(median(ppgs), 2) if ppgs else None,
        "mean_ppg": round(mean(ppgs), 2) if ppgs else None,
        "p25_ppg": round(percentile(ppgs, 0.25), 2) if ppgs else None,
        "p75_ppg": round(percentile(ppgs, 0.75), 2) if ppgs else None,
        "median_points_per_million": round(median(per_million), 3) if per_million else None,
        "median_cost_per_point": (
            round(1.0 / median(per_million), 3) if per_million and median(per_million) > 0 else None
        ),
        # A tier built on a handful of games is reported but not treated as a
        # baseline — the flag travels with the number so it can't be read as
        # more than it is.
        "thin_sample": games < MIN_GAMES_FOR_BASELINE,
        "top_producers": [
            {"player_name": record["player_name"], "team": record["team"], "ppg": record["ppg"]}
            for record in sorted(members, key=lambda row: row["ppg"], reverse=True)[:5]
        ],
    }


def build_position_breakdown(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tier × position cut — $12M buys different production per position."""
    rows: list[dict[str, Any]] = []
    for tier in TIER_ORDER:
        for position in POSITION_GROUPS:
            members = [
                record for record in players
                if record["salary_tier"] == tier and record["position"] == position
            ]
            if not members:
                continue
            ppgs = [record["ppg"] for record in members]
            per_million = [
                record["points_per_million"] for record in members if record["points_per_million"] is not None
            ]
            rows.append(
                {
                    "tier": tier,
                    "label": TIER_LABELS.get(tier, tier),
                    "position": position,
                    "players": len(members),
                    "games": sum(record["games"] for record in members),
                    "median_ppg": round(median(ppgs), 2),
                    "median_points_per_million": round(median(per_million), 3) if per_million else None,
                }
            )
    return rows


# --------------------------------------------------------------------- slate


def build_slate_rows(
    *,
    players: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    tier_stats: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Contracted players who appear on today's slate, placed against their tier."""
    slate_context: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = candidate.get("player_key")
        if not key or key in slate_context:
            continue
        slate_context[key] = {
            "team": candidate.get("team", ""),
            "opponent": candidate.get("opponent", ""),
            "game_id": candidate.get("game_id", ""),
        }

    rows: list[dict[str, Any]] = []
    for record in players:
        context = slate_context.get(record["player_key"])
        if not context:
            continue
        tier = tier_stats.get(record["salary_tier"], {})
        tier_median_ppg = tier.get("median_ppg")
        tier_per_million = tier.get("median_points_per_million")

        rows.append(
            {
                "player_name": record["player_name"],
                "player_key": record["player_key"],
                "position": record["position"],
                "team": context["team"],
                "opponent": context["opponent"],
                "game_id": context["game_id"],
                "salary_tier": record["salary_tier"],
                "salary_tier_label": TIER_LABELS.get(record["salary_tier"], record["salary_tier"]),
                "apy": record["apy"],
                "apy_millions": record["apy_millions"],
                "estimated_salary": record["estimated_salary"],
                "games": record["games"],
                "seasons": record["seasons"],
                "ppg": record["ppg"],
                "points_per_million": record["points_per_million"],
                "tier_median_ppg": tier_median_ppg,
                "tier_median_points_per_million": tier_per_million,
                "vs_tier_median_pct": pct_delta(record["ppg"], tier_median_ppg),
                "value_index": (
                    round(record["points_per_million"] / tier_per_million, 3)
                    if record["points_per_million"] is not None and tier_per_million
                    else None
                ),
                "tier_thin_sample": bool(tier.get("thin_sample", True)),
                "season_splits": record["season_splits"],
            }
        )

    return sorted(rows, key=lambda row: (row["apy"], row["ppg"]), reverse=True)


def pct_delta(value: float, reference: float | None) -> float | None:
    if reference is None or reference == 0:
        return None
    return round((value - reference) / abs(reference) * 100.0, 1)


# ------------------------------------------------------------------- numerics


def mean(values: list[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float((ordered[middle - 1] + ordered[middle]) / 2)


def percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile — no numpy dependency needed here."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return float(ordered[lower] * (1 - weight) + ordered[upper] * weight)
