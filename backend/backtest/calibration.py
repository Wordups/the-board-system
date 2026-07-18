"""Pure calibration math for the backtest harness.

Every function operates on "resolved picks": dicts with at least
``model_prob`` (float, 0..1) and ``outcome`` (0 or 1). No I/O here —
this module is fully unit-testable with synthetic data.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable


def brier_score(picks: Iterable[dict[str, Any]]) -> float | None:
    """Mean squared error between model probability and binary outcome.

    0.0 is perfect, 0.25 is what an uninformed coin-flip forecaster scores
    on a balanced set. None when there are no picks.
    """
    total = 0.0
    n = 0
    for pick in picks:
        total += (float(pick["model_prob"]) - float(pick["outcome"])) ** 2
        n += 1
    if n == 0:
        return None
    return total / n


def summarize(picks: list[dict[str, Any]]) -> dict[str, Any]:
    """n, average model probability, actual hit rate, gap (pp), Brier."""
    n = len(picks)
    if n == 0:
        return {"n": 0, "avg_model_prob": None, "hit_rate": None, "gap_pp": None, "brier": None}
    avg_prob = sum(float(p["model_prob"]) for p in picks) / n
    hit_rate = sum(int(p["outcome"]) for p in picks) / n
    return {
        "n": n,
        "avg_model_prob": round(avg_prob, 4),
        "hit_rate": round(hit_rate, 4),
        "gap_pp": round((avg_prob - hit_rate) * 100.0, 1),
        "brier": round(brier_score(picks), 4),
    }


def bucket_table(
    picks: list[dict[str, Any]],
    key: Callable[[dict[str, Any]], str] = lambda p: str(p.get("market", "?")),
) -> dict[str, dict[str, Any]]:
    """Per-bucket calibration summaries, buckets ordered by descending n."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for pick in picks:
        buckets.setdefault(key(pick), []).append(pick)
    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return {name: summarize(rows) for name, rows in ordered}


def decile_index(prob: float) -> int:
    """Fixed-width decile bin for a probability: [0,0.1) -> 0 ... [0.9,1.0] -> 9."""
    prob = min(max(float(prob), 0.0), 1.0)
    return min(int(prob * 10.0), 9)


def decile_table(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ten-row calibration table over fixed probability deciles.

    Every decile row is present (n may be 0) so tables from different runs
    line up.
    """
    bins: list[list[dict[str, Any]]] = [[] for _ in range(10)]
    for pick in picks:
        bins[decile_index(pick["model_prob"])].append(pick)
    rows = []
    for i, bucket in enumerate(bins):
        stats = summarize(bucket)
        rows.append({"lo": i / 10.0, "hi": (i + 1) / 10.0, **stats})
    return rows


def flat_stake_pnl(bets: list[dict[str, Any]], stake: float = 5.0) -> dict[str, Any]:
    """P&L of flat-stake YES buys at the market's implied probability.

    Each bet needs ``implied_prob`` (entry price, 0..1 exclusive) and
    ``outcome`` (1 = side won). A win on a $s stake at price p pays
    s * (1 - p) / p; a loss costs the stake. Bets without a usable price
    are skipped (counted in ``skipped``).
    """
    n = wins = skipped = 0
    pnl = 0.0
    for bet in bets:
        price = bet.get("implied_prob")
        if price is None or not (0.0 < float(price) < 1.0):
            skipped += 1
            continue
        price = float(price)
        n += 1
        if int(bet["outcome"]) == 1:
            wins += 1
            pnl += stake * (1.0 - price) / price
        else:
            pnl -= stake
    staked = n * stake
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "skipped": skipped,
        "stake": stake,
        "staked": round(staked, 2),
        "pnl": round(pnl, 2),
        "roi": round(pnl / staked, 4) if staked else None,
    }
