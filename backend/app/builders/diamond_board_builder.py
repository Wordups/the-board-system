"""Diamond of the Day builder.

Adapted from the generated diamond package to the-board-system's real data shape:
the board's candidates use markets HR / Hits / TB / RBI / K, `player_name`,
`sim_prob_pct` (0-100), tiers A/B/C — and this repo is model/edge-driven with NO
book odds, so there is no edge/american (kept in the contract as 0.0 / null).

It is a *view* over already-scored rows (games[].markets) — no new sim/scoring.
Assembles: 1B table-setter (Hits/TB/RBI) · 2B/3B HR · HOME swing (HR-biased) ·
MOUND K, with HR floor >= 2. Output matches the generated package's
`diamond_to_json` contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

HR_FLOOR = 2
BASE_MARKETS = ("Hits", "TB", "RBI")
HOME_HR_BIAS = 2.0  # score points: prefer a HR within this of the top swing
TIER_REASON = {"A": "Strong", "B": "Lean", "C": "Value"}


@dataclass
class DiamondPick:
    position: str
    name: str
    team: str
    market: str
    prob: float            # 0..1 (sim clear probability)
    edge: float            # 0.0 — no book odds in this repo
    american: Optional[int]  # None — no book odds
    reasoning: str
    is_hr: bool = False
    # display extras (the-board-system frontend uses these)
    player_id: str = ""
    opponent: str = ""
    line: str = ""
    tier: str = ""
    score: float = 0.0
    sim_prob_pct: Optional[float] = None


@dataclass
class Diamond:
    date: str
    early_or_late: str
    picks: dict[str, DiamondPick] = field(default_factory=dict)
    hr_count: int = 0
    hr_floor: int = HR_FLOOR
    is_valid: bool = False
    error: Optional[str] = None


def _score(row: dict) -> float:
    return float(row.get("score") or 0.0)


def _prob01(row: dict) -> float:
    sp = row.get("sim_prob_pct")
    return float(sp) / 100.0 if sp is not None else 0.0


def _rank_prob(row: dict) -> float:
    sp = row.get("sim_prob_pct")
    return float(sp) if sp is not None else _score(row)


def _flatten(games: list[dict]) -> list[dict]:
    rows = []
    for game in games:
        matchup = game.get("matchup", "")
        for market, players in (game.get("markets") or {}).items():
            for player in players or []:
                rows.append({**player, "market": market, "matchup": matchup})
    return rows


def _to_pick(row: dict, position: str, reasoning: str) -> DiamondPick:
    is_hr = row["market"] == "HR"
    return DiamondPick(
        position=position,
        name=row.get("player_name", "?"),
        team=row.get("team", "?"),
        market=row.get("market", ""),
        prob=round(_prob01(row), 4),
        edge=0.0,
        american=None,
        reasoning=reasoning,
        is_hr=is_hr,
        player_id=str(row.get("player_id", "")),
        opponent=row.get("opponent", ""),
        line=row.get("line", ""),
        tier=row.get("tier", ""),
        score=row.get("score", 0.0),
        sim_prob_pct=row.get("sim_prob_pct"),
    )


def build_diamond(games: list[dict], date: str = "", early_or_late: str = "EARLY") -> Diamond:
    diamond = Diamond(date=date, early_or_late=early_or_late)
    rows = _flatten(games)
    if not rows:
        diamond.error = "No candidates"
        return diamond

    hr_rows = [r for r in rows if r["market"] == "HR"]
    base_rows = [r for r in rows if r["market"] in BASE_MARKETS]
    k_rows = [r for r in rows if r["market"] == "K"]
    used: set[str] = set()

    def best(pool, key):
        avail = [r for r in pool if r.get("player_name") not in used]
        return max(avail, key=key) if avail else None

    # 1B — table-setter: safest base by simulated probability
    first = best(base_rows, _rank_prob)
    if first:
        used.add(first["player_name"])
        diamond.picks["1B"] = _to_pick(first, "1B", "Table-setter: highest-prob Hit/TB base")
    # 2B — solid HR by model score
    second = best(hr_rows, _score)
    if second:
        used.add(second["player_name"])
        diamond.picks["2B"] = _to_pick(second, "2B", "Solid HR by model score")
    # 3B — next HR by simulated probability
    third = best(hr_rows, _rank_prob)
    if third:
        used.add(third["player_name"])
        diamond.picks["3B"] = _to_pick(third, "3B", "Value HR by simulated probability")
    # HOME — swing: best remaining HR/TB by score, HR-biased
    swing = [r for r in rows if r["market"] in ("HR", "TB") and r.get("player_name") not in used]
    if swing:
        top = max(swing, key=_score)
        if top["market"] != "HR":
            near_hr = [r for r in swing if r["market"] == "HR" and _score(top) - _score(r) <= HOME_HR_BIAS]
            home = max(near_hr, key=_score) if near_hr else top
        else:
            home = top
        used.add(home["player_name"])
        diamond.picks["HOME"] = _to_pick(home, "HOME", "Swing: boldest remaining HR/TB")
    # MOUND — best pitcher K
    mound = best(k_rows, _rank_prob)
    if mound:
        used.add(mound["player_name"])
        diamond.picks["MOUND"] = _to_pick(mound, "MOUND", "Best pitcher K")

    def hr_count() -> int:
        return sum(1 for s in ("1B", "2B", "3B", "HOME")
                   if s in diamond.picks and diamond.picks[s].is_hr)

    # HR floor: swap a non-HR base for the best available HR until met
    while hr_count() < HR_FLOOR:
        repl = best(hr_rows, _score)
        if not repl:
            break
        target = next((s for s in ("1B", "HOME", "3B", "2B")
                       if s in diamond.picks and not diamond.picks[s].is_hr), None)
        if target is None:
            break
        used.discard(diamond.picks[target].name)
        used.add(repl["player_name"])
        diamond.picks[target] = _to_pick(repl, target, "HR (floor enforced)")

    diamond.hr_count = hr_count()
    diamond.is_valid = diamond.hr_count >= diamond.hr_floor
    return diamond


def diamond_to_json(diamond: Diamond) -> dict[str, Any]:
    return {
        "title": "Diamond of the Day",
        "date": diamond.date,
        "early_or_late": diamond.early_or_late,
        "picks": {
            pos: {
                "position": pick.position,
                "player_id": pick.player_id,
                "name": pick.name,
                "team": pick.team,
                "opponent": pick.opponent,
                "market": pick.market,
                "line": pick.line,
                "prob": round(pick.prob, 4),
                "edge": round(pick.edge, 4),
                "american": pick.american,
                "tier": pick.tier,
                "score": pick.score,
                "sim_prob_pct": pick.sim_prob_pct,
                "reasoning": pick.reasoning,
                "is_hr": pick.is_hr,
            }
            for pos, pick in diamond.picks.items()
        },
        "hr_count": diamond.hr_count,
        "hr_floor": diamond.hr_floor,
        "is_valid": diamond.is_valid,
        "error": diamond.error,
    }
