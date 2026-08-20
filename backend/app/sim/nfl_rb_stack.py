"""Same-PLAYER RB stat-stack correlation sim for the NFL board.

Sibling of nfl_qb_stack.py, same shape, different position and different
three markets: one running back's own Rush Yards, Receiving Yards, and
Anytime TD, correlated with each other in the same game. A back who gets
more touches on a given Sunday tends to rack up rushing yards AND receiving
yards AND scoring chances together -- more work, more of every kind of
production -- and a naive independent stack (P(RushYds) * P(RecYds) *
P(TD)) throws that shared "he had a monster workload" signal away, same
problem nfl_qb_stack.py already solves for a QB's three passing markets.

Deliberately its own module (not nfl_qb_stack.py, not nfl_same_game.py) even
though the correlation MECHANISM is identical to nfl_qb_stack.py (a one-
factor Gaussian copula blended into three otherwise-independent marginal
draws) -- the markets, the RHO reasoning, the floor/ceiling construction,
and the qualifying-candidate extraction are all position-specific enough
that folding this into nfl_qb_stack.py would make that module about "QB and
RB stat stacks" instead of just "QB", which is a worse read for both. The
_normal_cdf and _poisson_ppf math helpers ARE reused directly from
nfl_qb_stack.py (imported, not re-typed) since that math has nothing
QB-specific about it.

THE CORRELATION MODEL
----------------------
Each market keeps its OWN already-calibrated marginal distribution exactly
as scored elsewhere in the pipeline (nfl_model.normal_at_least for
RushYds/RecYds, nfl_model.poisson_at_least for TD) -- this module never
touches how those probabilities are individually computed. What's new is
correlating the three markets' *simulated draws* via a one-factor Gaussian
copula, identical mechanism to nfl_qb_stack.py:

1. Per Monte Carlo iteration, draw four independent standard normals:
   z_shared, z_rush, z_rec, z_td.
2. Blend each market's own idiosyncratic shock with the shared one:
       z_market_final = sqrt(RHO) * z_shared + sqrt(1 - RHO) * z_market
   RHO is the fraction of each market's variance attributed to the shared
   driver. See RHO's docstring below for why this module picks a different
   value than nfl_qb_stack.py's 0.65.
3. Convert each blended z to a uniform quantile via the standard normal CDF
   (nfl_qb_stack._normal_cdf, math.erf-based -- same technique
   nfl_model.normal_at_least already uses).
4. Realize each market from its own marginal at that quantile:
   - RushYds, RecYds (Normal-modeled): mean + std * z_final directly --
     already Normal, no quantile inversion needed.
   - TD (Poisson-modeled, the combined rush+rec anytime-TD lambda from
     nfl_model.project_td_lambda): the quantile is inverted through the
     Poisson CDF (nfl_qb_stack._poisson_ppf, reused directly).

Because all three z_final draws share the same z_shared, a good iteration
for one market tends to be a good iteration for the others too -- exactly
the real-world "he had 20 touches today" correlation a naive independent
stack can't express, while each market's own marginal distribution (mean,
std / lambda) is left completely untouched by the blend.

Pure math + a plain-dict candidate interface -- no ESPN I/O -- independently
unit-testable against fixed synthetic inputs, same convention as
nfl_qb_stack.py.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

import numpy as np

from app.models import nfl_model
from app.sim.nfl_qb_stack import _normal_cdf, _poisson_ppf

# Iteration count / seed mirror nfl_qb_stack.py's seeded, deterministic
# convention (np.random.default_rng(seed), not the global RNG) so results
# are reproducible and testable.
DEFAULT_ITERATIONS = 20_000
DEFAULT_SEED = 20260819

# The load-bearing modeling decision in this module, and it's deliberately
# NOT the same number as nfl_qb_stack.py's RHO=0.65. A QB's PassYds,
# Completions, and PassTD are all downstream of one thing happening in one
# phase of the game (dropbacks, in the air, same throws) -- mechanically
# close cousins. An RB's three markets span two DIFFERENT phases (the run
# game and the passing game) plus a TD lambda that blends both: a workhorse,
# three-down back's rush and rec roles genuinely do move together (more
# offensive snaps -> more of everything), but a committee/passing-down-
# specialist back's rec role can swing largely independent of his rush
# role -- and game script actively pulls the two APART for some backs
# (a big early lead -> more clock-control rushes, fewer garbage-time
# checkdowns; a big deficit -> the opposite), a decoupling force nfl_qb_
# stack.py's three passing markets don't have an equivalent of. RHO=0.55
# encodes "still a real positive shared-usage driver, materially widening
# the joint over the naive independent product" without claiming RB rush
# and receiving production are as mechanically fused as a QB's three
# passing markets are. Same reasoning shape and tone as nfl_qb_stack.py's
# own RHO note and app/scoring/prob_shrinkage.py's NO_SHRINK_SPORTS
# docstring: a deliberate, documented constant chosen from domain reasoning,
# NOT fit to any backtest -- there is no NFL backtest yet (collector shipped
# 2026-08-19, first real slate is Week 1 2026-09-09) to fit one against.
# Revisit once real NFL grades exist.
RHO = 0.55

# RushYds/RecYds are scored in nfl_collector.py via nfl_model.normal_at_least
# with NO std_ratio/min_std override (unlike PassYds/Completions, which do
# override) -- so the collector implicitly uses normal_at_least's own
# defaults. Named here rather than left as a silent assumption so this
# module's re-simulated std matches the collector's real scoring exactly;
# if normal_at_least's defaults ever change, this constant (and the
# marginal-preservation test) will visibly need updating with it, rather
# than silently drifting the way a hand-copied duplicate default could.
RB_YDS_STD_RATIO = 0.55
RB_YDS_MIN_STD = 15.0

# A "big game" ceiling threshold for RushYds/RecYds is generated by calling
# nfl_model.yardage_line() again against the same mean with a HIGHER share
# than the floor line used. The collector posts both RushYds and RecYds
# floor lines at yardage_line()'s own default share (0.82) -- the exact
# same default PassYds's floor line uses in nfl_qb_stack.py's setup -- so
# this module reuses nfl_qb_stack.py's identical CEILING_SHARE=1.15 rather
# than inventing a new constant for what is, mechanically, the same "roughly
# as far above the mean as the floor share sits below it" stretch-goal gap.
CEILING_SHARE = 1.15

# TD only ever posts one line on the board ("Anytime TD", i.e. threshold
# 1+) -- there's no PassTD-style ladder for it in the collector. But a
# bellcow back's 2+ TD read can be a real, distinct ceiling play the same
# way PassTD's higher rungs are for a high-volume passer, so this module
# derives its own 2+ rung directly from the already-tagged td_lambda,
# surfacing it as the ceiling leg only when it clears the same "still a
# legible longshot, not noise" floor nfl_collector.py's PASS_TD_MIN_RUNG_
# PROBABILITY / INTERCEPTIONS_MIN_RUNG_PROBABILITY constants use (0.05).
# Falls back to 1+ (== the floor rung) when 2+ doesn't clear that bar, so
# ceiling never posts a rung nobody would call a real read.
TD_CEILING_MIN_PROBABILITY = 0.05

_LEADING_INT_RE = re.compile(r"^(\d+)\+")


def _seed_for(game_id: str, rb_player_id: str, base_seed: int = DEFAULT_SEED) -> int:
    # Own key namespace ("rb_stack") so an RB's seed here never collides with
    # any other simulation of the same player/game elsewhere in the pipeline
    # -- same isolation nfl_qb_stack.py's _seed_for gives its own "qb_stack"
    # namespace.
    key = f"{game_id}:{rb_player_id}:rb_stack:{base_seed}".encode()
    return int(hashlib.blake2b(key, digest_size=8).hexdigest(), 16) % (2**32)


def _parse_leading_int(line: str) -> int | None:
    match = _LEADING_INT_RE.match(str(line).strip())
    return int(match.group(1)) if match else None


def extract_rb_stat_stack(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pull one team's lead RB's RushYds/RecYds/TD material out of a flat
    candidate list. Requires all three markets to be present for the same
    RB (a posted RushYds row, a posted RecYds row, and a posted TD row) --
    a partial trio (e.g. RushYds but no RecYds, because that back's
    receiving mean fell under the collector's 8-yard gate) isn't a stat
    *stack*, so this returns None rather than building one from two
    markets. If more than one RB on the team somehow qualifies for all
    three legs (e.g. an even committee split), takes the back with the
    higher combined rush+rec mean -- same "pick the standout" tie-break
    nfl_same_game.py's extract_qb_ladder uses for pass_td_lambda."""
    by_player: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        if str(cand.get("position")) != "RB":
            continue
        pid = str(cand.get("player_id"))
        entry = by_player.setdefault(pid, {"player_id": pid, "player_name": cand.get("player_name")})
        market = cand.get("market")
        if market == "RushYds" and "rush_yds_row" not in entry:
            entry["rush_yds_row"] = cand
        elif market == "RecYds" and "rec_yds_row" not in entry:
            entry["rec_yds_row"] = cand
        elif market == "TD" and "td_row" not in entry:
            entry["td_row"] = cand

    qualifying: list[dict[str, Any]] = []
    for pid, entry in by_player.items():
        rush_row = entry.get("rush_yds_row")
        rec_row = entry.get("rec_yds_row")
        td_row = entry.get("td_row")
        if rush_row is None or rec_row is None or td_row is None:
            continue

        rush_yds_mean = rush_row.get("rush_yds_mean")
        rec_yds_mean = rec_row.get("rec_yds_mean")
        td_lambda = td_row.get("td_lambda")
        if rush_yds_mean is None or rec_yds_mean is None or td_lambda is None:
            continue

        floor_rush_yds_line = _parse_leading_int(rush_row.get("line", ""))
        floor_rec_yds_line = _parse_leading_int(rec_row.get("line", ""))
        if floor_rush_yds_line is None or floor_rec_yds_line is None:
            continue

        qualifying.append(
            {
                "player_id": pid,
                "player_name": entry["player_name"],
                "rush_yds_mean": float(rush_yds_mean),
                "floor_rush_yds_line": floor_rush_yds_line,
                "rec_yds_mean": float(rec_yds_mean),
                "floor_rec_yds_line": floor_rec_yds_line,
                "td_lambda": float(td_lambda),
            }
        )

    if not qualifying:
        return None
    return max(qualifying, key=lambda entry: entry["rush_yds_mean"] + entry["rec_yds_mean"])


def simulate_rb_stat_stack(
    *,
    rush_yds_mean: float,
    rush_yds_std: float,
    rec_yds_mean: float,
    rec_yds_std: float,
    td_lambda: float,
    rho: float = RHO,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Monte Carlo the one-factor Gaussian-copula blend described in this
    module's docstring. Returns (rush_yds_samples, rec_yds_samples,
    td_samples), each shape (iterations,)."""
    rng = np.random.default_rng(seed)
    z_shared = rng.standard_normal(iterations)
    z_rush = rng.standard_normal(iterations)
    z_rec = rng.standard_normal(iterations)
    z_td = rng.standard_normal(iterations)

    sqrt_rho = math.sqrt(rho)
    sqrt_idio = math.sqrt(1.0 - rho)

    z_rush_final = sqrt_rho * z_shared + sqrt_idio * z_rush
    z_rec_final = sqrt_rho * z_shared + sqrt_idio * z_rec
    z_td_final = sqrt_rho * z_shared + sqrt_idio * z_td

    rush_yds_samples = rush_yds_mean + rush_yds_std * z_rush_final
    rec_yds_samples = rec_yds_mean + rec_yds_std * z_rec_final

    td_quantiles = _normal_cdf(z_td_final)
    td_samples = np.fromiter(
        (_poisson_ppf(td_lambda, q) for q in td_quantiles),
        dtype=np.int64,
        count=iterations,
    )

    return rush_yds_samples, rec_yds_samples, td_samples


def _joint_prob(
    rush_yds_samples: np.ndarray,
    rec_yds_samples: np.ndarray,
    td_samples: np.ndarray,
    *,
    rush_yds_line: float,
    rec_yds_line: float,
    td_rung: int,
) -> float:
    hits = (
        (rush_yds_samples >= rush_yds_line)
        & (rec_yds_samples >= rec_yds_line)
        & (td_samples >= td_rung)
    )
    return float(np.count_nonzero(hits)) / len(rush_yds_samples)


def build_rb_stack_for_team(
    *,
    game_id: str,
    matchup: str,
    candidates: list[dict[str, Any]],
    iterations: int = DEFAULT_ITERATIONS,
    seed: int | None = None,
    rho: float = RHO,
) -> list[dict[str, Any]]:
    """Build a floor/ceiling RB stat-stack card for one team's lead RB
    within a single game's candidate list. Returns [] (not an error) if no
    RB has a qualifying posted RushYds + RecYds + TD trio."""
    stack = extract_rb_stat_stack(candidates)
    if stack is None:
        return []

    rush_yds_std = max(RB_YDS_MIN_STD, stack["rush_yds_mean"] * RB_YDS_STD_RATIO)
    rec_yds_std = max(RB_YDS_MIN_STD, stack["rec_yds_mean"] * RB_YDS_STD_RATIO)

    resolved_seed = seed if seed is not None else _seed_for(game_id, stack["player_id"])
    rush_yds_samples, rec_yds_samples, td_samples = simulate_rb_stat_stack(
        rush_yds_mean=stack["rush_yds_mean"],
        rush_yds_std=rush_yds_std,
        rec_yds_mean=stack["rec_yds_mean"],
        rec_yds_std=rec_yds_std,
        td_lambda=stack["td_lambda"],
        rho=rho,
        iterations=iterations,
        seed=resolved_seed,
    )

    ceiling_rush_yds_line = nfl_model.yardage_line(stack["rush_yds_mean"], round_to=5, share=CEILING_SHARE)
    ceiling_rec_yds_line = nfl_model.yardage_line(stack["rec_yds_mean"], round_to=5, share=CEILING_SHARE)

    floor_td_rung = 1
    ceiling_td_rung = (
        2 if nfl_model.poisson_at_least(stack["td_lambda"], 2) >= TD_CEILING_MIN_PROBABILITY else 1
    )

    floor_joint = _joint_prob(
        rush_yds_samples,
        rec_yds_samples,
        td_samples,
        rush_yds_line=stack["floor_rush_yds_line"],
        rec_yds_line=stack["floor_rec_yds_line"],
        td_rung=floor_td_rung,
    )
    ceiling_joint = _joint_prob(
        rush_yds_samples,
        rec_yds_samples,
        td_samples,
        rush_yds_line=ceiling_rush_yds_line,
        rec_yds_line=ceiling_rec_yds_line,
        td_rung=ceiling_td_rung,
    )

    return [
        {
            "game_id": str(game_id),
            "matchup": matchup,
            "rb": {"player_name": stack["player_name"], "player_id": str(stack["player_id"])},
            "floor": {
                "rush_yds_line": f"{stack['floor_rush_yds_line']}+ Rush Yds",
                "rec_yds_line": f"{stack['floor_rec_yds_line']}+ Rec Yds",
                "td_line": f"{floor_td_rung}+ TD",
                "joint_prob_pct": round(floor_joint * 100.0, 1),
            },
            "ceiling": {
                "rush_yds_line": f"{ceiling_rush_yds_line}+ Rush Yds",
                "rec_yds_line": f"{ceiling_rec_yds_line}+ Rec Yds",
                "td_line": f"{ceiling_td_rung}+ TD",
                "joint_prob_pct": round(ceiling_joint * 100.0, 1),
            },
        }
    ]


def build_rb_stacks_for_game(
    *,
    game_id: str,
    matchup: str,
    candidates: list[dict[str, Any]],
    iterations: int = DEFAULT_ITERATIONS,
) -> list[dict[str, Any]]:
    """Run the sim independently per team within a game (each team has its
    own lead RB) and return the combined list of stack cards."""
    stacks: list[dict[str, Any]] = []
    teams = sorted({str(cand.get("team")) for cand in candidates if cand.get("team")})
    for team in teams:
        team_candidates = [cand for cand in candidates if str(cand.get("team")) == team]
        stacks.extend(
            build_rb_stack_for_team(
                game_id=game_id,
                matchup=matchup,
                candidates=team_candidates,
                iterations=iterations,
            )
        )
    return stacks
