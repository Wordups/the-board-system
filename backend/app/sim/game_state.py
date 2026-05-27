"""Game-state distributions (Phase 12, Component 2).

Each board generation samples a game state per simulation rather than assuming
every game is "normal". State is sampled once per sim; outcome models in
``outcome_models`` own the downstream effect of each state on player volume and
rate (a blowout means something different for a starting pitcher's K total than
for a hitter's TB total).

Conditioning hooks reweight the base distribution from optional context keys
(spread, back_to_back, pitch_count_risk). Phase 8 inputs do not populate these
yet, so the base distribution is returned unchanged until Phase 13 collectors
add them — the hooks are wired now so the upgrade is drop-in.
"""

from __future__ import annotations

import numpy as np

# State order is fixed; outcome-model effect tables index by position.
MLB_STATES: tuple[str, ...] = (
    "normal",                    # 0  pitcher 5-7 IP, lineup turns over normally
    "pitcher_pulled_early",      # 1  starter gone <=4 IP, bullpen exposure
    "pitcher_dominant",          # 2  7+ IP, low pitch count
    "weather",                   # 3  wind / rain delay
    "bullpen_game",              # 4  opener / committee
    "position_player_pitching",  # 5  blowout mop-up
)
MLB_BASE_PROBS = np.array([0.60, 0.15, 0.10, 0.08, 0.05, 0.02])

NBA_STATES: tuple[str, ...] = (
    "normal",           # 0  within 12 at 4Q start
    "blowout_for",      # 1  player's team up 15+
    "blowout_against",  # 2  player's team down 15+
    "foul_trouble",     # 3
    "rest",             # 4  load management / 18-24 min
    "overtime",         # 5
)
NBA_BASE_PROBS = np.array([0.55, 0.12, 0.12, 0.10, 0.08, 0.03])

_SPORT_STATES: dict[str, tuple[tuple[str, ...], np.ndarray]] = {
    "MLB": (MLB_STATES, MLB_BASE_PROBS),
    "NBA": (NBA_STATES, NBA_BASE_PROBS),
    "WNBA": (NBA_STATES, NBA_BASE_PROBS),  # basketball — same state structure as NBA
}


def state_probs(sport: str, context: dict | None = None) -> np.ndarray | None:
    """Conditional state distribution for ``sport``; ``None`` if unmodeled."""
    entry = _SPORT_STATES.get(sport)
    if entry is None:
        return None
    probs = entry[1].copy()
    ctx = context or {}
    if sport == "MLB":
        probs = _condition_mlb(probs, ctx)
    elif sport in ("NBA", "WNBA"):
        probs = _condition_nba(probs, ctx)
    total = probs.sum()
    return probs / total if total > 0 else entry[1]


def _condition_mlb(probs: np.ndarray, ctx: dict) -> np.ndarray:
    risk = float(ctx.get("pitch_count_risk") or 0.0)  # 0..1, starter early-exit risk
    if risk > 0:
        probs[1] *= 1.0 + risk            # pitcher_pulled_early more likely
        probs[2] *= max(0.0, 1.0 - risk)  # pitcher_dominant less likely
    return probs


def _condition_nba(probs: np.ndarray, ctx: dict) -> np.ndarray:
    spread = abs(float(ctx.get("spread") or 0.0))
    if spread > 12:
        probs[1] *= 3.0
        probs[2] *= 3.0
    elif spread > 8:
        probs[1] *= 2.0
        probs[2] *= 2.0
    if ctx.get("back_to_back"):
        probs[4] *= 2.0  # rest more likely on a back-to-back
    return probs


def sample_state_indices(
    rng: np.random.Generator,
    n: int,
    sport: str,
    context: dict | None = None,
) -> np.ndarray:
    """Vectorized draw of ``n`` state indices. Unmodeled sports → all "normal" (0)."""
    probs = state_probs(sport, context)
    if probs is None:
        return np.zeros(n, dtype=np.intp)
    return rng.choice(len(probs), size=n, p=probs)
