"""Same-game QB <-> pass-catcher correlation sim for the NFL board.

The original design doc (docs/superpowers/specs/2026-08-19-nfl-correlation-
board-design.md) sketched a full drive-level "possession -> scoring type ->
player attribution" Monte Carlo, mirroring MLB's sim engine. That's a
deliberate non-goal here: the NFL collector only ingests season-aggregate
per-game rates (see app/collectors/nfl_collector.py) — there's no
play-by-play or drive-count data feeding this pipeline, so a drive simulator
would have to invent its own possession-count/pace assumptions from nothing.

Instead this module builds a **compound Poisson + multinomial attribution**
model matched to the data granularity that actually exists:

1. A team's total passing-TD count for the game is
   ``N ~ Poisson(qb_pass_td_lambda)`` — the QB's already-computed
   ``project_pass_td_lambda`` output (see nfl_collector.build_team_player_
   candidates, which tags it onto each of the QB's PassTD-ladder candidate
   rows as ``pass_td_lambda``). No new lambda is invented here.
2. Each of those N TD passes is independently attributed to one of the
   team's pass-catchers (WR/TE/RB) via a weighted lottery — weight is that
   catcher's shrunk receiving-TD rate (``nfl_model.shrunk_rate`` against
   ``nfl_model.POSITION_PRIORS[...]["rec_td"]``), normalized across the
   team's pass-catchers that game.
3. Monte Carlo draws N then the multinomial attribution many times, and
   tallies each catcher's simulated TD-catch count per iteration.

The resulting joint (team-TD-count, catcher-TD-count) sample is a genuine
simulated distribution — receivers are negatively correlated with each
other for a fixed N, and every receiver's count is positively correlated
with the QB's total. That's exactly the correlation a naive
P(QB rung) * P(receiver rung) independence assumption throws away.

Pure math + a plain-dict candidate interface — no ESPN I/O — so this is
independently unit-testable against fixed synthetic inputs. Deliberately
its own module (not sim_engine.py, which is MLB-specific and drive-based).
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np

from app.models.nfl_model import POSITION_PRIORS, shrunk_rate

# Iteration count / seed mirror sim_engine.py's seeded, deterministic
# convention (np.random.default_rng(seed), not the global RNG) so results
# are reproducible and testable.
DEFAULT_ITERATIONS = 20_000
DEFAULT_SEED = 20260512

PASS_CATCHER_POSITIONS = {"RB", "WR", "TE"}

# A receiver TD-count threshold is a "goes with" pairing for a given QB rung
# when its simulated P(receiver >= j | QB clears that rung) lands in this
# band around the target — neither a near-certainty nor a longshot relative
# to the QB clearing his rung.
PAIRING_BAND = (0.30, 0.70)
PAIRING_TARGET = 0.50
RECEIVER_THRESHOLDS = (1, 2, 3)

_PASS_TD_RUNG_RE = re.compile(r"^(\d+)\+\s*Pass TD$")


def _parse_pass_td_rung(line: str) -> int | None:
    match = _PASS_TD_RUNG_RE.match(line.strip())
    return int(match.group(1)) if match else None


def extract_qb_ladder(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pull the team's starting-QB PassTD ladder out of a flat candidate
    list. Returns {'player_id', 'player_name', 'pass_td_lambda', 'rungs'}
    (rungs sorted ascending) or None if no qualifying QB ladder exists."""
    by_player: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        if cand.get("market") != "PassTD":
            continue
        rung = _parse_pass_td_rung(str(cand.get("line", "")))
        if rung is None:
            continue
        pid = str(cand["player_id"])
        entry = by_player.setdefault(
            pid,
            {
                "player_id": pid,
                "player_name": cand["player_name"],
                "pass_td_lambda": cand.get("pass_td_lambda"),
                "rungs": [],
            },
        )
        entry["rungs"].append(rung)

    qualifying = [
        entry for entry in by_player.values()
        if entry["pass_td_lambda"] is not None and entry["rungs"]
    ]
    if not qualifying:
        return None
    # A team should only ever produce one starting-QB ladder per game; if
    # more than one somehow qualifies, take the higher-lambda one.
    qb = max(qualifying, key=lambda entry: entry["pass_td_lambda"])
    qb["rungs"] = sorted(set(qb["rungs"]))
    return qb


def extract_pass_catchers(candidates: list[dict[str, Any]], *, exclude_player_id: str) -> list[dict[str, Any]]:
    """Distinct WR/TE/RB players (by player_id) in the candidate list, each
    with the raw fields needed for the shrunk rec_td rate: position,
    games_played, rec_td_per_game."""
    by_player: dict[str, dict[str, Any]] = {}
    for cand in candidates:
        pid = str(cand["player_id"])
        if pid == exclude_player_id:
            continue
        position = cand.get("position")
        if position not in PASS_CATCHER_POSITIONS:
            continue
        if pid in by_player:
            continue
        by_player[pid] = {
            "player_id": pid,
            "player_name": cand["player_name"],
            "position": position,
            "games_played": float(cand.get("games_played", 0.0) or 0.0),
            "rec_td_per_game": float(cand.get("rec_td_per_game", 0.0) or 0.0),
        }
    return list(by_player.values())


def catcher_attribution_weights(catchers: list[dict[str, Any]]) -> np.ndarray:
    """Normalized attribution weights across a team's pass-catchers, from
    each catcher's shrunk receiving-TD rate. Falls back to a uniform split
    if every catcher's shrunk rate is (degenerately) zero."""
    rates = np.array(
        [
            shrunk_rate(
                catcher["rec_td_per_game"],
                POSITION_PRIORS.get(catcher["position"], POSITION_PRIORS["WR"])["rec_td"],
                catcher["games_played"],
            )
            for catcher in catchers
        ],
        dtype=float,
    )
    total = rates.sum()
    if total <= 0:
        return np.full(len(catchers), 1.0 / len(catchers))
    return rates / total


def _seed_for(game_id: str, qb_player_id: str, base_seed: int = DEFAULT_SEED) -> int:
    import hashlib

    key = f"{game_id}:{qb_player_id}:{base_seed}".encode()
    return int(hashlib.blake2b(key, digest_size=8).hexdigest(), 16) % (2**32)


def simulate_team_passing_game(
    *,
    pass_td_lambda: float,
    catcher_weights: np.ndarray,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> tuple[np.ndarray, np.ndarray]:
    """Monte Carlo the compound-Poisson + multinomial-attribution model.

    Returns (team_td_counts, catcher_td_counts): team_td_counts has shape
    (iterations,); catcher_td_counts has shape (iterations, n_catchers).
    """
    rng = np.random.default_rng(seed)
    n_catchers = len(catcher_weights)
    team_td_counts = rng.poisson(max(0.0, pass_td_lambda), size=iterations)
    catcher_td_counts = np.zeros((iterations, n_catchers), dtype=np.int64)

    max_n = int(team_td_counts.max()) if iterations else 0
    # Draw the multinomial attribution once per unique N value (grouping
    # iterations that share the same team TD count) rather than once per
    # iteration — same result, far fewer numpy calls for a full slate.
    for n in range(1, max_n + 1):
        mask = team_td_counts == n
        count = int(mask.sum())
        if count == 0:
            continue
        catcher_td_counts[mask] = rng.multinomial(n, catcher_weights, size=count)

    return team_td_counts, catcher_td_counts


def _best_receiver_threshold(
    team_td_counts: np.ndarray,
    receiver_counts: np.ndarray,
    qb_rung: int,
) -> tuple[int, float] | None:
    """Pick the receiver TD-count threshold j whose simulated
    P(receiver >= j | QB clears qb_rung) lands closest to PAIRING_TARGET
    within PAIRING_BAND. Returns (j, joint_probability) or None if the QB
    never clears the rung across the whole simulated sample."""
    qb_hit = team_td_counts >= qb_rung
    n_qb_hit = int(qb_hit.sum())
    if n_qb_hit == 0:
        return None

    in_band: list[tuple[float, int, float]] = []  # (distance, j, joint_prob)
    fallback: tuple[int, float] | None = None
    for j in RECEIVER_THRESHOLDS:
        receiver_hit = receiver_counts >= j
        joint_prob = float(np.count_nonzero(qb_hit & receiver_hit)) / len(team_td_counts)
        conditional = float(np.count_nonzero(receiver_hit[qb_hit])) / n_qb_hit
        if fallback is None:
            fallback = (j, joint_prob)
        if PAIRING_BAND[0] <= conditional <= PAIRING_BAND[1]:
            in_band.append((abs(conditional - PAIRING_TARGET), j, joint_prob))

    if in_band:
        in_band.sort(key=lambda row: row[0])
        _, j, joint_prob = in_band[0]
        return j, joint_prob
    # Nothing landed in-band (e.g. a low-volume receiver never clears even
    # 1+ within band, or a workhorse always does) — fall back to the
    # lowest threshold so a card is still produced rather than dropped.
    return fallback


def build_same_game_pairs_for_team(
    *,
    game_id: str,
    matchup: str,
    candidates: list[dict[str, Any]],
    iterations: int = DEFAULT_ITERATIONS,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Build a floor/ceiling same-game correlation pair for one team's QB +
    its top pass-catcher (by attribution share) within a single game's
    candidate list. Returns [] (not an error) if there's no qualifying QB
    ladder or no attributable pass-catcher on the team."""
    qb = extract_qb_ladder(candidates)
    if qb is None:
        return []
    catchers = extract_pass_catchers(candidates, exclude_player_id=qb["player_id"])
    if not catchers:
        return []

    weights = catcher_attribution_weights(catchers)
    resolved_seed = seed if seed is not None else _seed_for(game_id, qb["player_id"])
    team_td_counts, catcher_td_counts = simulate_team_passing_game(
        pass_td_lambda=qb["pass_td_lambda"],
        catcher_weights=weights,
        iterations=iterations,
        seed=resolved_seed,
    )

    top_idx = int(np.argmax(weights))
    receiver = catchers[top_idx]
    receiver_counts = catcher_td_counts[:, top_idx]

    floor_rung = qb["rungs"][0]
    ceiling_rung = qb["rungs"][-1]

    floor_pick = _best_receiver_threshold(team_td_counts, receiver_counts, floor_rung)
    ceiling_pick = _best_receiver_threshold(team_td_counts, receiver_counts, ceiling_rung)
    if floor_pick is None or ceiling_pick is None:
        return []

    floor_j, floor_joint = floor_pick
    ceiling_j, ceiling_joint = ceiling_pick

    return [
        {
            "game_id": str(game_id),
            "matchup": matchup,
            "qb": {"player_name": qb["player_name"], "player_id": str(qb["player_id"])},
            "receiver": {"player_name": receiver["player_name"], "player_id": str(receiver["player_id"])},
            "floor": {
                "qb_line": f"{floor_rung}+ Pass TD",
                "receiver_line": f"{floor_j}+ Rec TD",
                "joint_prob_pct": round(floor_joint * 100.0, 1),
            },
            "ceiling": {
                "qb_line": f"{ceiling_rung}+ Pass TD",
                "receiver_line": f"{ceiling_j}+ Rec TD",
                "joint_prob_pct": round(ceiling_joint * 100.0, 1),
            },
        }
    ]


def build_same_game_pairs_for_game(
    *,
    game_id: str,
    matchup: str,
    candidates: list[dict[str, Any]],
    iterations: int = DEFAULT_ITERATIONS,
) -> list[dict[str, Any]]:
    """Run the sim independently per team within a game (each team has its
    own QB + pass-catcher pool) and return the combined list of pairs."""
    pairs: list[dict[str, Any]] = []
    teams = sorted({str(cand.get("team")) for cand in candidates if cand.get("team")})
    for team in teams:
        team_candidates = [cand for cand in candidates if str(cand.get("team")) == team]
        pairs.extend(
            build_same_game_pairs_for_team(
                game_id=game_id,
                matchup=matchup,
                candidates=team_candidates,
                iterations=iterations,
            )
        )
    return pairs
