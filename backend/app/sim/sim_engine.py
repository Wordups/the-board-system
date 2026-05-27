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

from app.sim.outcome_models import get_field, set_field, simulate


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


def simulate_candidates(candidates: Iterable, sport: str, config: SimConfig | None = None):
    """Set ``.sim_prob`` (0..1) on each candidate in place. Returns the candidates."""
    config = config or SimConfig()
    materialized = list(candidates)
    for candidate in materialized:
        set_field(candidate, "sim_prob", simulate_candidate(candidate, sport, config))
    return materialized


def sim_prob_pct(candidate) -> float | None:
    """Display form of a candidate's simulated probability, e.g. 67.3."""
    sim_prob = get_field(candidate, "sim_prob", None)
    return None if sim_prob is None else round(sim_prob * 100, 1)
