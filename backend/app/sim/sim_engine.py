"""Monte Carlo simulation engine (Phase 12, core).

Week 1 / parallel mode: runs alongside the deterministic edge score and emits a
``sim_prob`` per player-market. It does not touch the score, tiers, or board
display — Week 2 surfaces the probability, Week 3 backtests and calibrates.

Each player-market is simulated independently with a deterministic per-candidate
seed (reproducible golden tests). 2,500 sims are vectorized in NumPy — no
per-sim Python loop — so a full slate (~850 MLB player-markets) is well under
the 60s budget.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from app.sim.outcome_models import get_field, set_field, simulate, simulate_ladder


@dataclass(slots=True)
class SimConfig:
    n_sims: int = 2500          # locked (Phase 12 constraint)
    rel_std: float = 0.15       # v1 fixed relative std on float inputs (Option B)
    base_seed: int = 20260512


def _seed_for(candidate, base_seed: int) -> int:
    player_id = get_field(candidate, "player_id", "")
    market = get_field(candidate, "market", "")
    key = f"{player_id}:{market}:{base_seed}".encode()
    return int(hashlib.blake2b(key, digest_size=8).hexdigest(), 16) % (2**32)


def simulate_candidate(candidate, sport: str, config: SimConfig | None = None) -> float:
    """Simulated probability (0..1) that the candidate clears its line."""
    config = config or SimConfig()
    rng = np.random.default_rng(_seed_for(candidate, config.base_seed))
    return simulate(candidate, sport, rng, config.n_sims, config.rel_std)


def simulate_candidate_ladder(candidate, sport: str, config: SimConfig | None = None) -> dict[int, float] | None:
    """Whole-ladder survival probabilities {threshold: prob} for the candidate.

    Seeded identically to ``simulate_candidate``, so the ladder value at the
    headline rung reproduces ``sim_prob`` exactly. None for markets without a
    modeled ladder (see ``outcome_models.LADDER_RUNGS``).
    """
    config = config or SimConfig()
    rng = np.random.default_rng(_seed_for(candidate, config.base_seed))
    return simulate_ladder(candidate, sport, rng, config.n_sims, config.rel_std)


def simulate_candidates(candidates: Iterable, sport: str, config: SimConfig | None = None):
    """Set ``.sim_prob`` (0..1) on each candidate in place, plus ``.ladder``
    ({threshold: prob}) for laddered markets. Returns the candidates."""
    config = config or SimConfig()
    materialized = list(candidates)
    for candidate in materialized:
        set_field(candidate, "sim_prob", simulate_candidate(candidate, sport, config))
        ladder = simulate_candidate_ladder(candidate, sport, config)
        if ladder is not None:
            set_field(candidate, "ladder", ladder)
    return materialized


def sim_prob_pct(candidate) -> float | None:
    """Display form of a candidate's simulated probability, e.g. 67.3."""
    sim_prob = get_field(candidate, "sim_prob", None)
    return None if sim_prob is None else round(sim_prob * 100, 1)


def sim_prob_to_score(sim_prob: float | None) -> float | None:
    """Map a simulated clear probability (0..1) to the foundational 0..100 score.

    The board's headline number IS the model's actual probability: a 39.4%
    simulated HR scores 39.4. The mapping is deliberately the identity on the
    percentage scale (sim_prob * 100) so the score, the confidence, and the
    displayed probability are one and the same number. ~40% HR is the practical
    ceiling, so an elite HR naturally lands near the top of the range; markets
    with higher achievable probabilities (e.g. 1+ Hit) simply score higher,
    which is the intended, probability-honest behavior.

    Returns None when no sim probability exists (caller falls back to the edge
    score; see ``edge_score_to_score``).
    """
    if sim_prob is None:
        return None
    return round(max(0.0, min(1.0, float(sim_prob))) * 100.0, 2)
