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

# ---- Whole-ladder quoting (backlog #2: Copper-20+-vs-25+ lesson) ----
#
# Kalshi quotes point ladders per player (KXWNBAPTS 10+/15+/20+/25+/30+,
# KXMLBHR 1+/2+, ...). The model must quote every rung, not one headline
# line. Rung sets match Kalshi's live conventions (verified 2026-07-18):
# WNBA PTS ladders at 10/15/20/25/30; AST quotes odd rungs too (3+/5+/7+);
# REB quotes even rungs 2..12; MLB HR quotes 1+/2+.
LADDER_RUNGS: dict[str, tuple[int, ...]] = {
    "PTS": (10, 15, 20, 25, 30),
    "AST": (2, 3, 4, 5, 6, 7, 8),
    "REB": (2, 4, 6, 8, 10, 12),
    "HR": (1, 2),
}

# Per-market game-level std dev of the underlying stat, as (base, slope) on the
# headline line L: sigma = base + slope * L. v1 tunables (same spirit as
# rel_std) — e.g. a 20-point scorer swings ~6 pts game to game.
LADDER_SIGMA: dict[str, tuple[float, float]] = {
    "PTS": (2.0, 0.20),
    "AST": (1.0, 0.20),
    "REB": (1.2, 0.20),
}

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


def _mlb_hr_counts(candidate, sport, rng, n, rel_std) -> np.ndarray:
    """Simulated per-game HR counts (n,). Shared by the headline prob and the
    ladder so both read the identical sample paths from an identical rng."""
    extra = get_field(candidate, "extra", None) or {}
    p_game = min(max(_stat_value(candidate), _EPS), 1.0 - _EPS)
    pa_mean = float(extra.get("projected_pa") or 4.2)

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
    return rng.binomial(np.rint(pa_s).astype(int), rate_s)


def _mlb_hr(candidate, sport, rng, n, rel_std) -> float:
    threshold = _parse_threshold(get_field(candidate, "line", ""), 1)
    hr = _mlb_hr_counts(candidate, sport, rng, n, rel_std)
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


# ---------------------------------------------------------------- ladder sims


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    """Standard normal CDF (Abramowitz & Stegun 26.2.17, |err| < 7.5e-8).

    NumPy has no erf in core; scipy is not a dependency of this repo, so the
    polynomial approximation keeps the ladder dependency-free.
    """
    x = np.asarray(x, dtype=float)
    t = 1.0 / (1.0 + 0.2316419 * np.abs(x))
    poly = t * (
        0.319381530
        + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429)))
    )
    upper = 1.0 - (1.0 / np.sqrt(2.0 * np.pi)) * np.exp(-0.5 * x * x) * poly
    return np.where(x >= 0.0, upper, 1.0 - upper)


# Acklam's inverse-normal coefficients (~1.15e-9 relative error).
_PPF_A = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
          1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
_PPF_B = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
          6.680131188771972e01, -1.328068155288572e01)
_PPF_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
          -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
_PPF_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
          3.754408661907416e00)
_PPF_P_LOW = 0.02425


def _norm_ppf(p: np.ndarray) -> np.ndarray:
    """Standard normal inverse CDF (Acklam's algorithm), vectorized."""
    p = np.clip(np.asarray(p, dtype=float), _EPS, 1.0 - _EPS)
    out = np.empty_like(p)

    low = p < _PPF_P_LOW
    high = p > 1.0 - _PPF_P_LOW
    mid = ~(low | high)

    if np.any(mid):
        q = p[mid] - 0.5
        r = q * q
        num = ((((_PPF_A[0] * r + _PPF_A[1]) * r + _PPF_A[2]) * r + _PPF_A[3]) * r + _PPF_A[4]) * r + _PPF_A[5]
        den = ((((_PPF_B[0] * r + _PPF_B[1]) * r + _PPF_B[2]) * r + _PPF_B[3]) * r + _PPF_B[4]) * r + 1.0
        out[mid] = num * q / den

    for mask, prob, sign in ((low, p, 1.0), (high, 1.0 - p, -1.0)):
        if not np.any(mask):
            continue
        q = np.sqrt(-2.0 * np.log(prob[mask]))
        num = ((((_PPF_C[0] * q + _PPF_C[1]) * q + _PPF_C[2]) * q + _PPF_C[3]) * q + _PPF_C[4]) * q + _PPF_C[5]
        den = (((_PPF_D[0] * q + _PPF_D[1]) * q + _PPF_D[2]) * q + _PPF_D[3]) * q + 1.0
        out[mask] = -sign * num / den

    return out


def _ladder_thresholds(market: str, headline: int) -> list[int]:
    rungs = set(LADDER_RUNGS.get(market, ()))
    if headline > 0:
        rungs.add(headline)
    return sorted(rungs)


def _mlb_hr_ladder(candidate, sport, rng, n, rel_std) -> dict[int, float] | None:
    headline = _parse_threshold(get_field(candidate, "line", ""), 1)
    hr = _mlb_hr_counts(candidate, sport, rng, n, rel_std)
    return {t: float(np.mean(hr >= t)) for t in _ladder_thresholds("HR", headline)}


def _basketball_ladder(candidate, sport, rng, n, rel_std) -> dict[int, float] | None:
    """Survival probabilities at every rung of the market's line ladder.

    The headline rung replays the exact `_basketball_clear` mechanics (same
    rng call order, so an identically-seeded rng reproduces `sim_prob` to the
    bit). Other rungs shift each sim's effective clear probability through a
    latent-normal stat model anchored at the headline line:

        p_t = Phi(Phi^-1(p_eff) + (L - t) / sigma)

    with sigma the per-market game-level std dev (LADDER_SIGMA). Every rung is
    resolved against the SAME uniform draws, so rung events are nested and the
    ladder is monotone non-increasing by construction.
    """
    market = get_field(candidate, "market", "")
    headline = _parse_threshold(get_field(candidate, "line", ""), 0)
    if headline <= 0:
        return None

    central = _basketball_clear_prob(candidate)
    # Same call order as _bernoulli_clear: sample prob, states, then uniforms.
    p = _sample_prob(rng, central, n, rel_std)
    idx = sample_state_indices(rng, n, sport, _context(candidate))
    p_eff = np.clip(p * NBA_VOL[idx] * NBA_RATE[idx], _EPS, 1.0 - _EPS)
    u = rng.random(n)

    base, slope = LADDER_SIGMA[market]
    sigma = base + slope * headline
    z = _norm_ppf(p_eff)

    ladder: dict[int, float] = {}
    for t in _ladder_thresholds(market, headline):
        if t == headline:
            p_t = p_eff  # exact replay of the headline sim
        else:
            p_t = _norm_cdf(z + (headline - t) / sigma)
        ladder[t] = float(np.mean(u < p_t))
    return ladder


_LADDER_REGISTRY = {
    ("MLB", "HR"): _mlb_hr_ladder,
    **{(sport, market): _basketball_ladder
       for sport in ("NBA", "WNBA")
       for market in ("PTS", "AST", "REB")},
}


def simulate_ladder(candidate, sport: str, rng: np.random.Generator, n: int, rel_std: float) -> dict[int, float] | None:
    """{threshold: clear probability} across the market's standard rungs plus
    the headline line. None for markets without a modeled ladder."""
    model = _LADDER_REGISTRY.get((sport, get_field(candidate, "market")))
    if model is None:
        return None
    return model(candidate, sport, rng, n, rel_std)
