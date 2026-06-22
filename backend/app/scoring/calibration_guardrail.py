"""Calibration guardrail — scores each play against a market-specific closed-form
baseline and flags any play whose sim_prob has drifted too far above reality.

The Monte Carlo is a good SCREEN but a broken PRICER. This module sits between
the sim and the published board: right picks still get through; wrong PRICES get
flagged before they reach sizing, parlay math, or the front-end card grid.

Baselines by market (reliability noted in the docstring of each function):

  Market key  | Source                                  | Reliability
  ------------|-----------------------------------------|------------
  hits_1      | Binomial(AB, BA), P(X>=1)               | trustworthy
  hits_2      | Binomial(AB, BA), P(X>=2)               | trustworthy
  hr_1        | 1 - (1 - hr_per_pa)^PA                  | trustworthy
  tb_2        | DP over per-AB total-base distribution  | trustworthy
  k_9         | Poisson, lambda = (K/9) * expected IP   | reasonable
  rbi_2       | Poisson on RBI/game                     | WEAKEST (soft flag only)

RBI is context- and lineup-driven (depends on runners on base), not an
independent rate. Its gap is a soft signal that warns but never quarantines.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from math import comb, factorial
from typing import Any, Optional

# Default threshold + the set of "hard" markets whose gap is a hard kill.
# rbi_2 is in MARKET_KEYS but NOT in HARD_MARKETS — it gets a soft warning.
DEFAULT_THRESHOLD = 0.15
HARD_MARKETS = frozenset({"hits_1", "hits_2", "hr_1", "tb_2", "k_9"})
SOFT_MARKETS = frozenset({"rbi_2"})
MARKET_KEYS = HARD_MARKETS | SOFT_MARKETS


# ---------- market baseline models ----------

def p_at_least_k_hits(ba: float, ab: int, k: int) -> float:
    """P(X >= k), X ~ Binomial(ab, ba). Each at-bat is a hit w.p. = batting avg."""
    if ab <= 0 or ba <= 0:
        return 0.0
    if ba >= 1:
        return 1.0 if k <= ab else 0.0
    p_below = sum(comb(ab, i) * ba**i * (1 - ba) ** (ab - i) for i in range(k))
    return max(0.0, min(1.0, 1 - p_below))


def p_at_least_1_hr(hr_per_pa: float, pa: int) -> float:
    """P(>=1 HR) over `pa` plate appearances."""
    if pa <= 0 or hr_per_pa <= 0:
        return 0.0
    if hr_per_pa >= 1:
        return 1.0
    return 1 - (1 - hr_per_pa) ** pa


def p_at_least_k_strikeouts(k9: float, exp_ip: float, k: int) -> float:
    """P(>=k strikeouts), Poisson with lambda = (K/9) * expected innings."""
    lam = (k9 / 9.0) * exp_ip
    if lam <= 0:
        return 0.0
    cdf = sum(math.exp(-lam) * lam**i / factorial(i) for i in range(k))
    return max(0.0, min(1.0, 1 - cdf))


def p_at_least_k_rbi(rbi_per_game: float, k: int) -> float:
    """P(>=k RBI), Poisson proxy on per-game RBI rate. RBI depends on runners
    on base, not an independent rate — soft signal only (see SOFT_MARKETS)."""
    lam = rbi_per_game
    if lam <= 0:
        return 0.0
    cdf = sum(math.exp(-lam) * lam**i / factorial(i) for i in range(k))
    return max(0.0, min(1.0, 1 - cdf))


def p_at_least_2_tb(tb_dist: dict[int, float], ab: int) -> float:
    """P(total bases >= 2) over `ab` at-bats, given a per-AB distribution over
    {0,1,2,3,4} total bases. DP tracking P(sum==0), P(sum==1), P(sum>=2)."""
    if ab <= 0:
        return 0.0
    p0, p1, p2 = 1.0, 0.0, 0.0
    d = lambda v: tb_dist.get(v, 0.0)  # noqa: E731
    for _ in range(ab):
        n0 = p0 * d(0)
        n1 = p0 * d(1) + p1 * d(0)
        n2 = p2 + p0 * (d(2) + d(3) + d(4)) + p1 * (d(1) + d(2) + d(3) + d(4))
        p0, p1, p2 = n0, n1, n2
    return max(0.0, min(1.0, p2))


def tb_dist_from_line(
    ba: float, ab: int, doubles: int, triples: int, hr: int
) -> dict[int, float]:
    """Build a per-AB total-base distribution from a season line. The result
    is a probability mass function over {0,1,2,3,4} bases per AB and sums to
    1 by construction (singles is the residual of BA after XBH)."""
    if ab <= 0:
        return {0: 1.0, 1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
    p_double = doubles / ab
    p_triple = triples / ab
    p_hr = hr / ab
    p_single = max(0.0, ba - p_double - p_triple - p_hr)
    return {
        0: max(0.0, 1 - ba),
        1: p_single,
        2: p_double,
        3: p_triple,
        4: p_hr,
    }


# ---------- play container + market mapping ----------

@dataclass
class Play:
    """Input record for the guardrail. `market` is the spec key (hits_1, etc),
    not the board's market column. Use `market_key_from_board()` to translate."""

    name: str
    market: str
    sim_prob: float
    ba: Optional[float] = None
    ab_per_game: float = 4.0
    pa_per_game: float = 4.3
    hr_per_pa: Optional[float] = None
    k9: Optional[float] = None
    exp_ip: Optional[float] = None
    rbi_per_game: Optional[float] = None
    tb_dist: Optional[dict[int, float]] = None
    meta: dict[str, Any] = field(default_factory=dict)


def market_key_from_board(market: str, line: str) -> Optional[str]:
    """Translate the live board's (market, line) pair to a guardrail market key.

    Returns None for markets the guardrail does not model (e.g. MLB ML)."""
    line_norm = (line or "").strip().lower()
    if market == "HR":
        return "hr_1"
    if market == "Hits":
        if "2+" in line_norm:
            return "hits_2"
        if "1+" in line_norm:
            return "hits_1"
        return None
    if market == "TB":
        return "tb_2" if "2+" in line_norm else None
    if market == "K":
        return "k_9"  # threshold parsed from extra.k_threshold, not from this key
    if market == "RBI":
        return "rbi_2" if "2+" in line_norm else None  # rbi_1 not modeled
    return None


def baseline(play: Play) -> float:
    """Closed-form baseline probability for the play's market."""
    m = play.market
    ab = max(1, round(play.ab_per_game))
    if m == "hits_1":
        return p_at_least_k_hits(play.ba or 0.0, ab, 1)
    if m == "hits_2":
        return p_at_least_k_hits(play.ba or 0.0, ab, 2)
    if m == "hr_1":
        return p_at_least_1_hr(play.hr_per_pa or 0.0, max(1, round(play.pa_per_game)))
    if m == "k_9":
        threshold = int(play.meta.get("k_threshold", 9))
        return p_at_least_k_strikeouts(play.k9 or 0.0, play.exp_ip or 0.0, threshold)
    if m == "rbi_2":
        return p_at_least_k_rbi(play.rbi_per_game or 0.0, 2)
    if m == "tb_2":
        return p_at_least_2_tb(play.tb_dist or {}, ab)
    raise ValueError(f"unknown market: {m}")


# ---------- scorer + status classifier ----------

def status_for(market: str, gap: float, threshold: float) -> str:
    """One of: 'ok', 'flag' (hard quarantine), 'warn' (soft flag — rbi_2 only).

    Negative gaps (sim below baseline) never flag — the guardrail only catches
    inflated sims. A play coming in *under* its baseline is the sim being
    appropriately humble; let it through."""
    if gap <= threshold:
        return "ok"
    return "flag" if market in HARD_MARKETS else "warn"


def score_board(plays: list[Play], threshold: float = DEFAULT_THRESHOLD) -> list[dict]:
    """Score every play. Returns rows sorted by gap descending.

    Row keys: name, market, sim, baseline, gap, status ('ok'|'flag'|'warn'),
    flag (bool — True only for hard quarantine).
    """
    rows: list[dict] = []
    for play in plays:
        b = baseline(play)
        gap = play.sim_prob - b
        status = status_for(play.market, gap, threshold)
        rows.append(
            {
                "name": play.name,
                "market": play.market,
                "sim": play.sim_prob,
                "baseline": b,
                "gap": gap,
                "status": status,
                "flag": status == "flag",
            }
        )
    rows.sort(key=lambda r: r["gap"], reverse=True)
    return rows


def play_from_extra(name: str, market_key: str, sim_prob: float, extra: dict) -> Optional[Play]:
    """Construct a Play from a candidate.extra payload. Returns None when the
    extra dict is missing the inputs the market needs — that's a signal to skip
    scoring this play (the gap can't be computed; better silence than garbage)."""
    p = Play(name=name, market=market_key, sim_prob=sim_prob, meta=dict(extra or {}))
    e = extra or {}
    if market_key in ("hits_1", "hits_2"):
        if "season_ba" not in e or "ab_per_game" not in e:
            return None
        p.ba = float(e["season_ba"])
        p.ab_per_game = float(e["ab_per_game"])
    elif market_key == "hr_1":
        if "hr_per_pa" not in e or "pa_per_game" not in e:
            return None
        p.hr_per_pa = float(e["hr_per_pa"])
        p.pa_per_game = float(e["pa_per_game"])
    elif market_key == "tb_2":
        needed = {"season_ba", "ab_per_game", "season_doubles", "season_triples", "season_hr", "season_ab"}
        if not needed.issubset(e):
            return None
        ab_total = max(1, int(e["season_ab"]))
        p.ba = float(e["season_ba"])
        p.ab_per_game = float(e["ab_per_game"])
        p.tb_dist = tb_dist_from_line(
            ba=p.ba,
            ab=ab_total,
            doubles=int(e["season_doubles"]),
            triples=int(e["season_triples"]),
            hr=int(e["season_hr"]),
        )
    elif market_key == "k_9":
        if "k9" not in e or "exp_ip" not in e:
            return None
        p.k9 = float(e["k9"])
        p.exp_ip = float(e["exp_ip"])
        if "k_threshold" in e:
            p.meta["k_threshold"] = int(e["k_threshold"])
    elif market_key == "rbi_2":
        if "rbi_per_game" not in e:
            return None
        p.rbi_per_game = float(e["rbi_per_game"])
    else:
        return None
    return p


# ---------- debug helper (kept tiny — the spec's demo lives in tests) ----------

def format_row(row: dict) -> str:
    """Single-line render of a scored row, for diagnostic tables."""
    label = {"ok": "ok", "flag": "FLAG — inflated", "warn": "warn — soft"}[row["status"]]
    return (
        f"{row['name']:<22}{row['market']:<8}"
        f"{row['sim']*100:>6.1f}%"
        f"{row['baseline']*100:>7.1f}%"
        f"{row['gap']*100:>+7.1f}  {label}"
    )


if __name__ == "__main__":
    # Smoke check: the spec's three calibration cases.
    demo = [
        Play("Hunter Goodman", "hits_2", sim_prob=0.941, ba=0.242, ab_per_game=4.0),
        Play("Calibrated 1+H", "hits_1", sim_prob=0.730, ba=0.280, ab_per_game=4.0),
        Play("Inflated 1+H", "hits_1", sim_prob=0.940, ba=0.280, ab_per_game=4.0),
    ]
    rows = score_board(demo)
    print(f"{'PLAY':<22}{'MARKET':<8}{'SIM':>7}{'BASE':>8}{'GAP':>8}  STATUS")
    print("-" * 64)
    for r in rows:
        print(format_row(r))
