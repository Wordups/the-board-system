"""Same-PLAYER QB stat-stack correlation sim for the NFL board.

nfl_same_game.py correlates two different players (a QB and his top pass-
catcher) sharing one attribution draw. This module is a different shape:
one QB's three *own* passing markets — Passing Yards, Completions, Passing
TDs — correlated with each other in the same game. A QB who throws more and
sharper on a given Sunday racks up yards AND completions AND TDs together;
a naive independent stack (P(yds) * P(completions) * P(TD)) throws that
away and understates how often all three land on a big game (and overstates
it on a bad one). This module builds the real joint instead, via a
Gaussian-copula-style partial correlation across the three markets'
simulated draws.

Deliberately its own module (not nfl_same_game.py, not sim_engine.py which
is MLB-specific and drive-based) even though it shares the "same-game QB
correlation" theme — the correlation mechanism here (a shared driver blended
into three independent marginal draws) is structurally nothing like
same_game's compound-Poisson multinomial-attribution lottery, and mixing the
two into one file would make neither easy to read or test.

THE CORRELATION MODEL
----------------------
Each market keeps its OWN already-calibrated marginal distribution exactly
as scored elsewhere in the pipeline (nfl_model.normal_at_least for
PassYds/Completions, nfl_model.poisson_at_least for PassTD) — this module
never touches how those probabilities are individually computed. What's new
is correlating the three markets' *simulated draws* via a one-factor
Gaussian copula:

1. Per Monte Carlo iteration, draw four independent standard normals:
   z_shared, z_yds, z_comp, z_td.
2. Blend each market's own idiosyncratic shock with the shared one:
       z_market_final = sqrt(RHO) * z_shared + sqrt(1 - RHO) * z_market
   RHO is the fraction of each market's variance attributed to the shared
   driver. See RHO's docstring below for why 0.65 and not some other value.
3. Convert each blended z to a uniform quantile via the standard normal CDF
   (own _normal_cdf helper, math.erf-based — same technique
   nfl_model.normal_at_least already uses; no scipy in this repo's
   requirements.txt, and this doesn't need to be the thing that adds it).
4. Realize each market from its own marginal at that quantile:
   - PassYds, Completions (Normal-modeled): mean + std * z_final directly —
     already Normal, no quantile inversion needed.
   - PassTD (Poisson-modeled): the quantile is inverted through the Poisson
     CDF (own _poisson_ppf helper — manual cumulative-sum search, cheap
     since pass_td_lambda is small; mirrors the cumulative-sum style
     nfl_model.poisson_at_least already uses for the tail, just walking the
     CDF from k=0 instead of summing a fixed prefix).

Because all three z_final draws share the same z_shared, a good iteration
for one market tends to be a good iteration for the others too — exactly
the real-world "hot game" correlation a naive independent stack can't
express, while each market's own marginal distribution (mean, std / lambda)
is left completely untouched by the blend.

Pure math + a plain-dict candidate interface — no ESPN I/O — independently
unit-testable against fixed synthetic inputs, same convention as
nfl_same_game.py and nfl_model.py.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

import numpy as np

from app.collectors.nfl_collector import (
    COMPLETIONS_MIN_STD,
    COMPLETIONS_STD_RATIO,
    PASS_YDS_MIN_STD,
    PASS_YDS_STD_RATIO,
)
from app.models import nfl_model
from app.sim.nfl_same_game import extract_qb_ladder

# Iteration count / seed mirror nfl_same_game.py's seeded, deterministic
# convention (np.random.default_rng(seed), not the global RNG) so results
# are reproducible and testable.
DEFAULT_ITERATIONS = 20_000
DEFAULT_SEED = 20260512

# The load-bearing modeling decision in this whole module. These three QB
# markets are NOT the same event (a QB can rack up empty yards on checkdowns
# without matching TDs, or vulture a TD off a short field with a modest
# yardage day) — but they also aren't independent: they share a majority of
# their game-to-game variance through one common driver, pass volume and
# execution quality (more dropbacks, a defense that can't get pressure, a
# offense clicking through the air). RHO=0.65 encodes "strong positive
# correlation without claiming they're the same event" — high enough that
# the joint materially diverges from the naive independent product (the
# whole point of this module), not so high that a big passing-yards game is
# treated as guaranteeing a big completions/TD game too, which real NFL
# variance (garbage-time yards, red-zone stalls, a pick-six that caps a
# team's TD count despite a huge yardage day) doesn't support. Same
# reasoning shape and same tone as this repo's NFL shrinkage exemption
# (app/scoring/prob_shrinkage.py's NO_SHRINK_SPORTS docstring) and
# nfl_same_game.py's compound-Poisson design note: a deliberate, documented
# constant chosen from domain reasoning, NOT fit to any backtest — there is
# no NFL backtest yet (collector shipped 2026-08-19, first real slate is
# Week 1 2026-09-09) to fit one against. Revisit once real NFL grades exist.
RHO = 0.65

# A "big game" ceiling threshold for PassYds/Completions is generated by
# calling nfl_model.yardage_line() again against the same mean with a HIGHER
# share than the floor line used (0.82 for PassYds, 0.85 for Completions —
# see nfl_collector.py's QB block). 1.15 mirrors those floor shares'
# distance below 1.0 on the other side: roughly as far above the mean as the
# floor line sits below it, giving a "big game" threshold that's a genuine
# stretch goal rather than a token bump.
CEILING_SHARE = 1.15

# Safety cap on the Poisson-CDF inversion walk in _poisson_ppf — a quantile
# this far into the tail of a small-lambda Poisson (QB pass_td_lambda tops
# out well under 5) is already numerically indistinguishable from 1.0; this
# just bounds the loop rather than ever actually being reached in practice.
_POISSON_PPF_MAX_K = 200

_LEADING_INT_RE = re.compile(r"^(\d+)\+")


def _normal_cdf(z: np.ndarray) -> np.ndarray:
    """Standard normal CDF, vectorized. Same math.erf technique
    nfl_model.normal_at_least uses for the complementary (survival) form:
    Phi(z) = 0.5 * (1 + erf(z / sqrt(2)))."""
    erf = np.vectorize(math.erf)
    return 0.5 * (1.0 + erf(z / math.sqrt(2.0)))


def _poisson_ppf(lam: float, quantile: float) -> int:
    """Smallest count k such that P(Poisson(lam) <= k) >= quantile.

    Manual cumulative-sum walk of the Poisson PMF rather than a scipy
    ppf/isf call (no scipy in requirements.txt) — cheap since lam is always
    a QB's per-game pass_td_lambda (well under 5 in practice)."""
    lam = max(0.0, lam)
    if lam == 0.0:
        return 0
    cumulative = 0.0
    pmf = math.exp(-lam)
    k = 0
    while k < _POISSON_PPF_MAX_K:
        cumulative += pmf
        if cumulative >= quantile:
            return k
        k += 1
        pmf *= lam / k
    return _POISSON_PPF_MAX_K


def _seed_for(game_id: str, qb_player_id: str, base_seed: int = DEFAULT_SEED) -> int:
    # Own key namespace ("qb_stack") so a QB's seed here never collides with
    # (and can't be mistaken for) nfl_same_game.py's independently-seeded
    # simulation of the same QB in the same game — two different Monte
    # Carlos over two different correlation shapes, deliberately decoupled.
    key = f"{game_id}:{qb_player_id}:qb_stack:{base_seed}".encode()
    return int(hashlib.blake2b(key, digest_size=8).hexdigest(), 16) % (2**32)


def extract_qb_stat_stack(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pull one team's starting QB's PassYds/Completions/PassTD material out
    of a flat candidate list. Requires all three markets to be present for
    the same QB (via extract_qb_ladder for the PassTD ladder, plus a posted
    PassYds row and a posted Completions row) — a partial pair (e.g. a
    PassTD ladder but no PassYds candidate, because that QB's mean fell
    under the collector's 100-yard gate) isn't a stat *stack*, so this
    returns None rather than building one from two markets."""
    qb = extract_qb_ladder(candidates)
    if qb is None:
        return None
    qb_id = qb["player_id"]

    pass_yds_row: dict[str, Any] | None = None
    completions_row: dict[str, Any] | None = None
    for cand in candidates:
        if str(cand.get("player_id")) != qb_id:
            continue
        market = cand.get("market")
        if market == "PassYds" and pass_yds_row is None:
            pass_yds_row = cand
        elif market == "Completions" and completions_row is None:
            completions_row = cand
    if pass_yds_row is None or completions_row is None:
        return None

    pass_yds_mean = pass_yds_row.get("pass_yds_mean")
    completions_mean = completions_row.get("completions_mean")
    if pass_yds_mean is None or completions_mean is None:
        return None

    floor_pass_yds_line = _parse_leading_int(pass_yds_row.get("line", ""))
    floor_completions_line = _parse_leading_int(completions_row.get("line", ""))
    if floor_pass_yds_line is None or floor_completions_line is None:
        return None

    return {
        "player_id": qb_id,
        "player_name": qb["player_name"],
        "pass_td_lambda": qb["pass_td_lambda"],
        "floor_pass_td_rung": qb["rungs"][0],
        "ceiling_pass_td_rung": qb["rungs"][-1],
        "pass_yds_mean": float(pass_yds_mean),
        "floor_pass_yds_line": floor_pass_yds_line,
        "completions_mean": float(completions_mean),
        "floor_completions_line": floor_completions_line,
    }


def _parse_leading_int(line: str) -> int | None:
    match = _LEADING_INT_RE.match(str(line).strip())
    return int(match.group(1)) if match else None


def simulate_qb_stat_stack(
    *,
    pass_yds_mean: float,
    pass_yds_std: float,
    completions_mean: float,
    completions_std: float,
    pass_td_lambda: float,
    rho: float = RHO,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Monte Carlo the one-factor Gaussian-copula blend described in this
    module's docstring. Returns (pass_yds_samples, completions_samples,
    pass_td_samples), each shape (iterations,)."""
    rng = np.random.default_rng(seed)
    z_shared = rng.standard_normal(iterations)
    z_yds = rng.standard_normal(iterations)
    z_comp = rng.standard_normal(iterations)
    z_td = rng.standard_normal(iterations)

    sqrt_rho = math.sqrt(rho)
    sqrt_idio = math.sqrt(1.0 - rho)

    z_yds_final = sqrt_rho * z_shared + sqrt_idio * z_yds
    z_comp_final = sqrt_rho * z_shared + sqrt_idio * z_comp
    z_td_final = sqrt_rho * z_shared + sqrt_idio * z_td

    pass_yds_samples = pass_yds_mean + pass_yds_std * z_yds_final
    completions_samples = completions_mean + completions_std * z_comp_final

    td_quantiles = _normal_cdf(z_td_final)
    pass_td_samples = np.fromiter(
        (_poisson_ppf(pass_td_lambda, q) for q in td_quantiles),
        dtype=np.int64,
        count=iterations,
    )

    return pass_yds_samples, completions_samples, pass_td_samples


def _joint_prob(
    pass_yds_samples: np.ndarray,
    completions_samples: np.ndarray,
    pass_td_samples: np.ndarray,
    *,
    pass_yds_line: float,
    completions_line: float,
    pass_td_rung: int,
) -> float:
    hits = (
        (pass_yds_samples >= pass_yds_line)
        & (completions_samples >= completions_line)
        & (pass_td_samples >= pass_td_rung)
    )
    return float(np.count_nonzero(hits)) / len(pass_yds_samples)


def build_qb_stack_for_team(
    *,
    game_id: str,
    matchup: str,
    candidates: list[dict[str, Any]],
    iterations: int = DEFAULT_ITERATIONS,
    seed: int | None = None,
    rho: float = RHO,
) -> list[dict[str, Any]]:
    """Build a floor/ceiling QB stat-stack card for one team's starting QB
    within a single game's candidate list. Returns [] (not an error) if the
    QB doesn't have a qualifying PassTD ladder + posted PassYds + posted
    Completions trio."""
    stack = extract_qb_stat_stack(candidates)
    if stack is None:
        return []

    pass_yds_std = max(PASS_YDS_MIN_STD, stack["pass_yds_mean"] * PASS_YDS_STD_RATIO)
    completions_std = max(COMPLETIONS_MIN_STD, stack["completions_mean"] * COMPLETIONS_STD_RATIO)

    resolved_seed = seed if seed is not None else _seed_for(game_id, stack["player_id"])
    pass_yds_samples, completions_samples, pass_td_samples = simulate_qb_stat_stack(
        pass_yds_mean=stack["pass_yds_mean"],
        pass_yds_std=pass_yds_std,
        completions_mean=stack["completions_mean"],
        completions_std=completions_std,
        pass_td_lambda=stack["pass_td_lambda"],
        rho=rho,
        iterations=iterations,
        seed=resolved_seed,
    )

    ceiling_pass_yds_line = nfl_model.yardage_line(stack["pass_yds_mean"], round_to=5, share=CEILING_SHARE)
    ceiling_completions_line = nfl_model.yardage_line(
        stack["completions_mean"], round_to=1, share=CEILING_SHARE, minimum=5
    )

    floor_joint = _joint_prob(
        pass_yds_samples,
        completions_samples,
        pass_td_samples,
        pass_yds_line=stack["floor_pass_yds_line"],
        completions_line=stack["floor_completions_line"],
        pass_td_rung=stack["floor_pass_td_rung"],
    )
    ceiling_joint = _joint_prob(
        pass_yds_samples,
        completions_samples,
        pass_td_samples,
        pass_yds_line=ceiling_pass_yds_line,
        completions_line=ceiling_completions_line,
        pass_td_rung=stack["ceiling_pass_td_rung"],
    )

    return [
        {
            "game_id": str(game_id),
            "matchup": matchup,
            "qb": {"player_name": stack["player_name"], "player_id": str(stack["player_id"])},
            "floor": {
                "pass_yds_line": f"{stack['floor_pass_yds_line']}+ Pass Yds",
                "completions_line": f"{stack['floor_completions_line']}+ Completions",
                "pass_td_line": f"{stack['floor_pass_td_rung']}+ Pass TD",
                "joint_prob_pct": round(floor_joint * 100.0, 1),
            },
            "ceiling": {
                "pass_yds_line": f"{ceiling_pass_yds_line}+ Pass Yds",
                "completions_line": f"{ceiling_completions_line}+ Completions",
                "pass_td_line": f"{stack['ceiling_pass_td_rung']}+ Pass TD",
                "joint_prob_pct": round(ceiling_joint * 100.0, 1),
            },
        }
    ]


def build_qb_stacks_for_game(
    *,
    game_id: str,
    matchup: str,
    candidates: list[dict[str, Any]],
    iterations: int = DEFAULT_ITERATIONS,
) -> list[dict[str, Any]]:
    """Run the sim independently per team within a game (each team has its
    own starting QB) and return the combined list of stack cards."""
    stacks: list[dict[str, Any]] = []
    teams = sorted({str(cand.get("team")) for cand in candidates if cand.get("team")})
    for team in teams:
        team_candidates = [cand for cand in candidates if str(cand.get("team")) == team]
        stacks.extend(
            build_qb_stack_for_team(
                game_id=game_id,
                matchup=matchup,
                candidates=team_candidates,
                iterations=iterations,
            )
        )
    return stacks
