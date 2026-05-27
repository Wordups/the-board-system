"""Per-market outcome simulators (Phase 12, Component 3).

Anchoring principle: under the "normal" game state with no input noise, every
model reproduces the existing point estimate. The simulation adds value through
(a) the game-state mixture, which is discrete and asymmetric, and (b) input
noise. The mixture is where the alpha lives — e.g. pitcher-K overs are dragged
down by early-hook risk, NBA overs by blowout/rest minutes risk.

Input semantics in the current pipeline differ by sport:
  - MLB HR carries counting inputs (projected_pa + per-game HR probability), so
    HR runs a true Binomial count simulation over plate appearances.
  - MLB Hits / TB / K / ML expose ``stat_value`` as an already-aggregated
    probability of clearing the line, with no underlying counts. These are
    simulated as a state-mixed Bernoulli on that probability. (True count
    simulation for these markets is blocked on richer collector outputs —
    Phase 13.)
  - NBA / WNBA candidates carry no ``stat_value``; they expose empirical
    ``l10_hit_rate`` / ``l5_hit_rate`` against the suggested line. The clear
    probability is a recency blend of those, then state-mixed (basketball
    minutes risk). Candidates here are plain dicts, not dataclasses, so every
    field access goes through ``get_field`` / ``set_field``.

v1 assumptions (Option B), centralized for tuning after the 30-day backtest:
  - Float inputs sampled at a fixed 15% relative std (``rel_std``).
  - Per-state volume/rate multipliers below.
"""

from __future__ import annotations

import re

import numpy as np

from app.sim.game_state import sample_state_indices

_EPS = 1e-6

# Per-state multipliers, indexed to MLB_STATES / NBA_STATES order.
# volume = opportunity (plate appearances / batters faced / minutes);
# rate = per-opportunity success rate.

# MLB batter (Hits / TB / HR).
MLB_HITTER_VOL = np.array([1.00, 1.03, 0.96, 1.00, 1.01, 1.05])
MLB_HITTER_RATE = np.array([1.00, 1.05, 0.86, 1.03, 1.04, 1.45])

# MLB pitcher (K) — same states read from the pitcher's side.
MLB_PITCHER_VOL = np.array([1.00, 0.55, 1.12, 0.95, 0.70, 1.00])
MLB_PITCHER_RATE = np.array([1.00, 0.92, 1.10, 0.98, 0.95, 1.00])

# NBA / WNBA player (basketball — same states for both leagues).
NBA_VOL = np.array([1.00, 0.82, 1.05, 0.80, 0.72, 1.12])
NBA_RATE = np.array([1.00, 1.05, 1.00, 0.95, 1.00, 1.00])


def get_field(candidate, key, default=None):
    """Read a field from either a dataclass candidate (MLB) or a dict (NBA/WNBA)."""
    if isinstance(candidate, dict):
        return candidate.get(key, default)
    return getattr(candidate, key, default)


def set_field(candidate, key, value) -> None:
    """Write a field to either a dataclass candidate or a dict."""
    if isinstance(candidate, dict):
        candidate[key] = value
    else:
        setattr(candidate, key, value)


def _parse_threshold(line: str, default: int = 1) -> int:
    match = re.search(r"\d+", line or "")
    return int(match.group()) if match else default


def _stat_value(candidate, default: float = 0.5) -> float:
    value = get_field(candidate, "stat_value", None)
    return default if value is None else float(value)


def _basketball_clear_prob(candidate) -> float:
    """Recency blend of empirical hit rates — the NBA/WNBA clear probability."""
    l10 = float(get_field(candidate, "l10_hit_rate", 0.0) or 0.0)
    l5 = float(get_field(candidate, "l5_hit_rate", 0.0) or 0.0)
    return 0.5 * l10 + 0.5 * l5


def _sample_prob(rng: np.random.Generator, mean: float, n: int, rel_std: float) -> np.ndarray:
    """Sample a probability ~ Normal(mean, rel_std*mean), clamped to (0, 1)."""
    samples = rng.normal(mean, rel_std * abs(mean), n)
    return np.clip(samples, _EPS, 1.0 - _EPS)


def _context(candidate) -> dict:
    extra = get_field(candidate, "extra", None) or {}
    def pick(key):
        # Prefer an explicit extra dict (MLB); fall back to a top-level field so
        # future basketball candidates carrying spread/back_to_back are honored.
        return extra.get(key, get_field(candidate, key, None))
    return {
        "spread": pick("spread"),
        "back_to_back": pick("back_to_back"),
        "pitch_count_risk": pick("pitch_count_risk"),
    }


def _bernoulli_clear(candidate, sport, rng, n, rel_std, central, vol_table, rate_table) -> float:
    """State-mixed Bernoulli on an already-aggregated clear probability."""
    p = _sample_prob(rng, central, n, rel_std)
    idx = sample_state_indices(rng, n, sport, _context(candidate))
    p_eff = np.clip(p * vol_table[idx] * rate_table[idx], _EPS, 1.0 - _EPS)
    return float(np.mean(rng.random(n) < p_eff))


def _mlb_hr(candidate, sport, rng, n, rel_std) -> float:
    extra = get_field(candidate, "extra", None) or {}
    p_game = min(max(_stat_value(candidate), _EPS), 1.0 - _EPS)
    pa_mean = float(extra.get("projected_pa") or 4.2)
    threshold = _parse_threshold(get_field(candidate, "line", ""), 1)

    idx = sample_state_indices(rng, n, sport, _context(candidate))
    # Back out the per-PA HR rate that reproduces the per-game probability at
    # mean PA, then perturb rate and PA per sim and resolve the count.
    base_rate = 1.0 - (1.0 - p_game) ** (1.0 / max(pa_mean, 1.0))
    rate_s = np.clip(
        base_rate * MLB_HITTER_RATE[idx] * rng.normal(1.0, rel_std, n),
        _EPS,
        1.0 - _EPS,
    )
    pa_s = np.clip(rng.normal(pa_mean, rel_std * pa_mean, n) * MLB_HITTER_VOL[idx], 0.0, 9.0)
    hr = rng.binomial(np.rint(pa_s).astype(int), rate_s)
    return float(np.mean(hr >= threshold))


def _mlb_hitter_clear(candidate, sport, rng, n, rel_std) -> float:
    return _bernoulli_clear(candidate, sport, rng, n, rel_std, _stat_value(candidate), MLB_HITTER_VOL, MLB_HITTER_RATE)


def _mlb_k(candidate, sport, rng, n, rel_std) -> float:
    return _bernoulli_clear(candidate, sport, rng, n, rel_std, _stat_value(candidate), MLB_PITCHER_VOL, MLB_PITCHER_RATE)


def _mlb_ml(candidate, sport, rng, n, rel_std) -> float:
    # Moneyline is a team outcome; game-state mixture does not apply. Noise only.
    p = _sample_prob(rng, _stat_value(candidate), n, rel_std)
    return float(np.mean(rng.random(n) < p))


def _basketball_clear(candidate, sport, rng, n, rel_std) -> float:
    # NBA/WNBA prop: clear probability from empirical hit rates, then minutes-risk
    # state mixture (blowout / foul trouble / rest drag overs down).
    return _bernoulli_clear(candidate, sport, rng, n, rel_std, _basketball_clear_prob(candidate), NBA_VOL, NBA_RATE)


def _basketball_ml(candidate, sport, rng, n, rel_std) -> float:
    # NBA/WNBA moneyline: win probability from recent win pct (stored as the hit
    # rates), team outcome so no game-state mixture. Noise only.
    p = _sample_prob(rng, _basketball_clear_prob(candidate) or 0.5, n, rel_std)
    return float(np.mean(rng.random(n) < p))


def _generic_model(candidate, sport, rng, n, rel_std) -> float:
    # Unmodeled sport/market: noise-only Bernoulli on the point estimate.
    p = _sample_prob(rng, _stat_value(candidate), n, rel_std)
    return float(np.mean(rng.random(n) < p))


_BASKETBALL_PROP_MODELS = {market: _basketball_clear for market in ("PTS", "AST", "REB", "3PM")}

_REGISTRY = {
    ("MLB", "HR"): _mlb_hr,
    ("MLB", "Hits"): _mlb_hitter_clear,
    ("MLB", "TB"): _mlb_hitter_clear,
    ("MLB", "RBI"): _mlb_hitter_clear,
    ("MLB", "K"): _mlb_k,
    ("MLB", "ML"): _mlb_ml,
    **{("NBA", market): model for market, model in _BASKETBALL_PROP_MODELS.items()},
    **{("WNBA", market): model for market, model in _BASKETBALL_PROP_MODELS.items()},
    ("NBA", "ML"): _basketball_ml,
    ("WNBA", "ML"): _basketball_ml,
}


def simulate(candidate, sport: str, rng: np.random.Generator, n: int, rel_std: float) -> float:
    """Return the simulated probability (0..1) of clearing the line."""
    model = _REGISTRY.get((sport, get_field(candidate, "market")), _generic_model)
    return model(candidate, sport, rng, n, rel_std)
