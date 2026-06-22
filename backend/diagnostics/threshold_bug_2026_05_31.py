"""Task 4 diagnostic — threshold bug vs calibration error.

Reads today's live MLB board, pulls every 1+ Hit / 2+ Hits play, and compares
its sim_prob_pct to the closed-form binomial baseline at several plausible BA
levels. If the 2+ Hits sims land near the 1+ Hit sims regardless of BA, the
sim's success counter is not enforcing the line threshold (threshold bug,
fixable in outcome_models). If the 2+ gaps come in smaller and varied, it's
pure calibration drift.

Run:  python -m diagnostics.threshold_bug_2026_05_31
"""

from __future__ import annotations

import json
from pathlib import Path

from app.scoring.calibration_guardrail import p_at_least_k_hits

REPO_ROOT = Path(__file__).resolve().parents[2]
MLB_JSON = REPO_ROOT / "backend" / "data_final" / "mlb.json"

# BA reference points. Real BA per player varies; the bug signature is that
# the 2+ sim doesn't budge much vs 1+ across ANY plausible BA.
BA_LEVELS = [0.220, 0.250, 0.280]
AB = 4  # standard top-of-order slate AB count


def collect_hits_plays(payload: dict) -> list[dict]:
    """Pull every Hits-market play out of pinned_board, consistency_board, and
    per-game market buckets. Skip duplicates by (player_name, line)."""
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    pools = [payload.get("pinned_board", {}).get("players", [])]
    pools.append(payload.get("consistency_board", {}).get("players", []) or [])
    for game in payload.get("games", []):
        for market, plays in (game.get("markets") or {}).items():
            if market == "Hits":
                pools.append(plays)
    for pool in pools:
        for play in pool or []:
            line = (play.get("line") or "").strip()
            if "Hit" not in line:
                continue
            key = (play.get("player_name", ""), line)
            if key in seen:
                continue
            seen.add(key)
            rows.append(play)
    return rows


def split_by_threshold(plays: list[dict]) -> tuple[list[dict], list[dict]]:
    """Bucket into 1+ Hit and 2+ Hits."""
    ones = [p for p in plays if "1+" in (p.get("line") or "")]
    twos = [p for p in plays if "2+" in (p.get("line") or "")]
    return ones, twos


def fmt_row(play: dict) -> str:
    name = play.get("player_name", "")[:24]
    line = play.get("line", "")[:8]
    sim = float(play.get("sim_prob_pct", 0) or 0)
    parts = [f"{name:<24}{line:<8}sim {sim:>5.1f}%"]
    for ba in BA_LEVELS:
        k = 2 if "2+" in line else 1
        base = p_at_least_k_hits(ba, AB, k) * 100
        gap = sim - base
        flag = "FLAG" if gap > 15 else "    "
        parts.append(f"BA={ba:.3f}: base {base:>5.1f}% gap {gap:>+5.1f}pp {flag}")
    return "  ".join(parts)


def gap_at(ba: float, k: int, sim_pct: float) -> float:
    return sim_pct - p_at_least_k_hits(ba, AB, k) * 100


def main() -> int:
    payload = json.loads(MLB_JSON.read_text(encoding="utf-8"))
    plays = collect_hits_plays(payload)
    ones, twos = split_by_threshold(plays)

    print(f"Calibration diagnostic — {payload.get('date', '?')} live MLB board")
    print(f"Source: {MLB_JSON.relative_to(REPO_ROOT)}")
    print(f"AB reference = {AB}; BA reference levels = {BA_LEVELS}")
    print()

    print(f"1+ Hit plays ({len(ones)}):")
    print("-" * 110)
    for play in sorted(ones, key=lambda p: -(p.get("sim_prob_pct") or 0))[:12]:
        print(fmt_row(play))
    print()

    print(f"2+ Hits plays ({len(twos)}):")
    print("-" * 110)
    for play in sorted(twos, key=lambda p: -(p.get("sim_prob_pct") or 0))[:12]:
        print(fmt_row(play))
    print()

    # Headline verdict math — median sim % per threshold bucket at BA=.250.
    def median(xs: list[float]) -> float:
        s = sorted(xs)
        n = len(s)
        return 0.0 if not n else (s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2]))

    one_sims = [float(p.get("sim_prob_pct") or 0) for p in ones]
    two_sims = [float(p.get("sim_prob_pct") or 0) for p in twos]
    print("VERDICT")
    print("-" * 110)
    print(f"  median 1+ Hit sim%  : {median(one_sims):.1f}%")
    print(f"  median 2+ Hits sim% : {median(two_sims):.1f}%")
    print(f"  expected at BA=.250 : 1+ Hit baseline = {p_at_least_k_hits(0.250, AB, 1)*100:.1f}%  /  2+ Hits = {p_at_least_k_hits(0.250, AB, 2)*100:.1f}%")
    if two_sims and one_sims:
        sim_delta = median(one_sims) - median(two_sims)
        base_delta = (p_at_least_k_hits(0.250, AB, 1) - p_at_least_k_hits(0.250, AB, 2)) * 100
        print(f"  observed sim delta  : {sim_delta:.1f}pp  (1+ minus 2+)")
        print(f"  expected baseline   : {base_delta:.1f}pp  (1+ minus 2+ at BA=.250)")
        if sim_delta < 5 and base_delta > 30:
            print()
            print("  ==> THRESHOLD BUG CONFIRMED. The sim's 1+ and 2+ sims are nearly")
            print("    identical despite the math demanding a ~40pp gap. The 2+ counter")
            print("    is not enforcing the line threshold; _mlb_hitter_clear() in")
            print("    backend/app/sim/outcome_models.py runs Bernoulli on stat_value")
            print("    without parsing the threshold from the line.")
            print()
            print("    Calibration guardrail will quarantine the inflated 2+ Hits plays")
            print("    until the counter is fixed in a follow-up PR (outside this PR's")
            print("    scope — affects every hits/TB/RBI play; high blast radius).")
        else:
            print()
            print("  ==> no clear threshold bug; gap looks like pure calibration drift.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
