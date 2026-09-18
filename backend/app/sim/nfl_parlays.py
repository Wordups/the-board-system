"""Same-game parlay generator for the NFL board.

Extends the QB/receiver correlation sim (nfl_same_game.py) to build full grind
(3-4 high-confidence legs) + moonshot (5-7 lower-confidence legs) tickets per game.

Applies game-script logic: shootout -> volume floors, blowout -> different strategy.
Handles cross-team stacking, prop type diversity (TD, yardage, receptions), and
anti-correlation gates (forbidden same-team pairings).
"""

from __future__ import annotations

from typing import Any
import numpy as np


def _calculate_game_script(spread: float, total: float) -> str:
    """Classify game script: shootout, blowout, or normal.

    Shootout: total >= 50 (high scoring likely)
    Blowout: spread >= 7 (lopsided matchup)
    Normal: everything else
    """
    if total is None or spread is None:
        return "normal"

    if total >= 50:
        return "shootout"
    if abs(spread) >= 7:
        return "blowout"
    return "normal"


def _extract_game_metrics(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Pull spread, total, and other game-level metrics from candidates."""
    metrics = {"spread": None, "total": None}
    for cand in candidates:
        if cand.get("game_spread") is not None:
            metrics["spread"] = float(cand["game_spread"])
        if cand.get("game_total") is not None:
            metrics["total"] = float(cand["game_total"])
        if metrics["spread"] is not None and metrics["total"] is not None:
            break
    return metrics


def _extract_players_by_team(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group candidates by team."""
    by_team: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        team = str(cand.get("team", ""))
        if team not in by_team:
            by_team[team] = []
        by_team[team].append(cand)
    return by_team


def _is_safe_pairing(player1: dict[str, Any], player2: dict[str, Any], game_script: str) -> bool:
    """Check if two players can be safely paired (no anti-correlations)."""
    team1 = str(player1.get("team", ""))
    team2 = str(player2.get("team", ""))
    pos1 = str(player1.get("position", ""))
    pos2 = str(player2.get("position", ""))

    # Same team? Forbidden pairings:
    # - RB rushing + QB passing TD (run script kills pass)
    # - RB receiving + QB rushing (rare but check anyway)
    if team1 == team2:
        if (pos1 == "RB" and pos2 == "QB") or (pos1 == "QB" and pos2 == "RB"):
            return False

    return True


def _score_player_for_grind(candidate: dict[str, Any], game_script: str) -> float:
    """Score a player for grind-ticket inclusion (high confidence, volume-based)."""
    score = float(candidate.get("score", 0))
    confidence = int(candidate.get("confidence", 0))
    market = str(candidate.get("market", ""))
    position = str(candidate.get("position", ""))

    base_score = score * confidence / 100.0

    # Volume metrics: yardage, receptions > TD in shootouts
    if game_script == "shootout":
        if market in ("PassYds", "RecYds", "REC"):
            base_score *= 1.2

    # Position preference for grind: QBs, WRs, dual-threat RBs
    if position in ("QB", "WR"):
        base_score *= 1.1

    return base_score


def _score_player_for_moonshot(candidate: dict[str, Any], game_script: str) -> float:
    """Score a player for moonshot (lottery layer, any-time TD)."""
    score = float(candidate.get("score", 0))
    confidence = int(candidate.get("confidence", 0))
    market = str(candidate.get("market", ""))
    position = str(candidate.get("position", ""))

    base_score = score * confidence / 100.0

    # Lottery layer: any-time TD, depth receivers
    if market == "TD" and confidence >= 60:
        base_score *= 1.3

    # Depth players: slot WRs, TEs
    if position in ("TE", "WR"):
        if market in ("RecYds", "REC"):
            base_score *= 1.15

    return base_score


def build_same_game_parlays_for_game(
    *,
    game_id: str,
    matchup: str,
    candidates: list[dict[str, Any]],
    grind_leg_count: int = 4,
    moonshot_leg_count: int = 14,
) -> list[dict[str, Any]]:
    """Build grind + moonshot parlay tickets for one game.

    Returns a list of parlay objects, each with:
    - game_id, matchup, game_script
    - ticket_type: 'grind' or 'moonshot'
    - legs: list of {player_name, player_id, market, line, score, confidence}
    - theme: description of the ticket strategy
    """
    if not candidates:
        return []

    # Gate: need at least some volume
    metrics = _extract_game_metrics(candidates)
    game_script = _calculate_game_script(metrics.get("spread"), metrics.get("total"))

    # Separate by market
    by_market: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        market = str(cand.get("market", "unknown"))
        if market not in by_market:
            by_market[market] = []
        by_market[market].append(cand)

    parlays: list[dict[str, Any]] = []

    # GRIND TICKET: 3-4 high-confidence, volume-based legs
    grind_candidates = sorted(
        candidates,
        key=lambda c: _score_player_for_grind(c, game_script),
        reverse=True,
    )
    grind_legs = []
    grind_players: set[str] = set()
    for cand in grind_candidates:
        if len(grind_legs) >= grind_leg_count:
            break
        pid = str(cand.get("player_id", ""))
        if pid in grind_players:
            continue
        # Anti-correlation gate
        is_safe = all(
            _is_safe_pairing(cand, existing_leg, game_script)
            for existing_leg in grind_legs
        )
        if not is_safe:
            continue
        grind_legs.append(cand)
        grind_players.add(pid)

    if len(grind_legs) >= 3:  # Only output if we hit min leg count
        parlays.append({
            "game_id": str(game_id),
            "matchup": matchup,
            "game_script": game_script,
            "ticket_type": "grind",
            "theme": f"Game script aligned: {game_script} → volume floors, cross-team correlation",
            "legs": [
                {
                    "player_name": leg.get("player_name", ""),
                    "player_id": str(leg.get("player_id", "")),
                    "market": leg.get("market", ""),
                    "line": leg.get("line", ""),
                    "score": round(float(leg.get("score", 0)), 2),
                    "confidence": int(leg.get("confidence", 0)),
                }
                for leg in grind_legs
            ],
        })

    # MOONSHOT TICKET: 5-7 lower-confidence, includes lottery (any-time TD)
    moonshot_candidates = sorted(
        candidates,
        key=lambda c: _score_player_for_moonshot(c, game_script),
        reverse=True,
    )
    moonshot_legs = []
    moonshot_players: set[str] = set()
    for cand in moonshot_candidates:
        if len(moonshot_legs) >= moonshot_leg_count:
            break
        pid = str(cand.get("player_id", ""))
        if pid in moonshot_players:
            continue
        # Anti-correlation gate
        is_safe = all(
            _is_safe_pairing(cand, existing_leg, game_script)
            for existing_leg in moonshot_legs
        )
        if not is_safe:
            continue
        moonshot_legs.append(cand)
        moonshot_players.add(pid)

    if len(moonshot_legs) >= 9:  # Only output if we hit min leg count (allow up to 14)
        parlays.append({
            "game_id": str(game_id),
            "matchup": matchup,
            "game_script": game_script,
            "ticket_type": "moonshot",
            "theme": f"Lottery layer: any-time TD + depth receivers, high-variance stacking",
            "legs": [
                {
                    "player_name": leg.get("player_name", ""),
                    "player_id": str(leg.get("player_id", "")),
                    "market": leg.get("market", ""),
                    "line": leg.get("line", ""),
                    "score": round(float(leg.get("score", 0)), 2),
                    "confidence": int(leg.get("confidence", 0)),
                }
                for leg in moonshot_legs
            ],
        })

    return parlays
