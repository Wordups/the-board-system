"""Same-game parlay generator for the NFL board.

Generates per-game tickets:
- Grind: 4 pure volume legs (PassYds, RecYds, RushYds, REC) — no TDs
- Moonshot: 2-3 star player any-time TDs + 11-12 volume legs (9-14 total)
- TD Parlay: Separate star-only ticket (Josh Allen, James Cook, top RB/WR)

Applies game-script logic: shootout -> volume floors, blowout -> RB focus.
Handles cross-team stacking and anti-correlation gates (forbidden same-team pairings).
"""

from __future__ import annotations

import math

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


def _identify_star_players(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Identify star players per team: QB, RB1, WR1, TE, RB2.

    Returns dict keyed by team, each with 'QB', 'RB1', 'WR1', 'TE', 'RB2'.
    Stars determined by highest score + confidence in their position.
    """
    by_team = _extract_players_by_team(candidates)
    stars = {}

    for team, players in by_team.items():
        stars[team] = {}

        # Find best QB
        qbs = [p for p in players if str(p.get("position", "")).upper() == "QB"]
        if qbs:
            stars[team]["QB"] = max(qbs, key=lambda p: float(p.get("score", 0)) * int(p.get("confidence", 1)) / 100)

        # Find RB1 and RB2
        rbs = [p for p in players if str(p.get("position", "")).upper() == "RB"]
        if rbs:
            sorted_rbs = sorted(rbs, key=lambda p: float(p.get("score", 0)) * int(p.get("confidence", 1)) / 100, reverse=True)
            if len(sorted_rbs) >= 1:
                stars[team]["RB1"] = sorted_rbs[0]
            if len(sorted_rbs) >= 2:
                stars[team]["RB2"] = sorted_rbs[1]

        # Find WR1
        wrs = [p for p in players if str(p.get("position", "")).upper() == "WR"]
        if wrs:
            stars[team]["WR1"] = max(wrs, key=lambda p: float(p.get("score", 0)) * int(p.get("confidence", 1)) / 100)

        # Find TE
        tes = [p for p in players if str(p.get("position", "")).upper() == "TE"]
        if tes:
            stars[team]["TE"] = max(tes, key=lambda p: float(p.get("score", 0)) * int(p.get("confidence", 1)) / 100)

    return stars


def _score_player_for_grind(candidate: dict[str, Any], game_script: str) -> float:
    """Score a player for grind-ticket inclusion (VOLUME ONLY, no TDs)."""
    score = float(candidate.get("score", 0))
    confidence = int(candidate.get("confidence", 0))
    market = str(candidate.get("market", ""))
    position = str(candidate.get("position", ""))

    # GRIND = PURE VOLUME: PassYds, RecYds, RushYds, REC only
    # No any-time TD scorers
    if market not in ("PassYds", "RecYds", "RushYds", "REC", "Completions"):
        return -999  # Exclude non-volume markets

    base_score = score * confidence / 100.0

    # Volume boost in shootouts
    if game_script == "shootout":
        base_score *= 1.3

    # Position preference: QB passing, WR receiving
    if market == "PassYds" and position == "QB":
        base_score *= 1.2
    elif market in ("RecYds", "REC") and position in ("WR", "TE"):
        base_score *= 1.2
    elif market in ("RushYds", "REC") and position == "RB":
        base_score *= 1.15

    return base_score


def _score_player_for_td(candidate: dict[str, Any], game_script: str, is_star: bool = False) -> float:
    """Score a player for any-time TD inclusion (moonshot lottery layer)."""
    score = float(candidate.get("score", 0))
    confidence = int(candidate.get("confidence", 0))
    market = str(candidate.get("market", ""))

    # Only TD market
    if market != "TD":
        return -999

    base_score = score * confidence / 100.0

    # Star players get huge boost
    if is_star:
        base_score *= 2.0

    return base_score


def _score_player_for_moonshot_volume(candidate: dict[str, Any], game_script: str) -> float:
    """Score a player for moonshot volume legs (non-TD)."""
    score = float(candidate.get("score", 0))
    confidence = int(candidate.get("confidence", 0))
    market = str(candidate.get("market", ""))
    position = str(candidate.get("position", ""))

    # Volume markets only for this scoring
    if market not in ("PassYds", "RecYds", "RushYds", "REC", "Completions"):
        return -999

    base_score = score * confidence / 100.0

    # Slight volume boost for moonshot
    if game_script == "shootout":
        base_score *= 1.2

    # Position bonus
    if market in ("RecYds", "REC") and position in ("WR", "TE"):
        base_score *= 1.15

    return base_score


def _reference_calibrated_odds(leg_count: int) -> int:
    """Estimate American SGP odds from the two supplied winning-slip anchors.

    The reference slips price 9 legs at +2363 (24.63 decimal) and 14 legs at
    +11496 (115.96 decimal).  Interpolating/extrapolating in log-decimal space
    preserves the compounding shape without pretending these are live book odds.
    """
    if leg_count <= 0:
        raise ValueError("leg_count must be positive")

    low_legs, low_decimal = 9, 24.63
    high_legs, high_decimal = 14, 115.96
    log_step = (math.log(high_decimal) - math.log(low_decimal)) / (high_legs - low_legs)
    decimal_odds = math.exp(math.log(low_decimal) + (leg_count - low_legs) * log_step)
    return round((decimal_odds - 1.0) * 100)


def _estimated_return(stake: float, american_odds: int) -> float:
    """Return total payout (stake included) for positive American odds."""
    return round(float(stake) * (1.0 + american_odds / 100.0), 2)


def build_same_game_parlays_for_game(
    *,
    game_id: str,
    matchup: str,
    time: str = "",
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build single ticket with grind + moonshot + TD parlay for one game.

    Returns one ticket object per game:
    - game_id, matchup, time, game_script
    - grind: {legs, odds, stake, implied_win}
    - moonshot: {legs, odds, stake, implied_win}
    - td_parlay: {legs, odds, stake, implied_win} — star players only
    """
    if not candidates:
        return None

    metrics = _extract_game_metrics(candidates)
    game_script = _calculate_game_script(metrics.get("spread"), metrics.get("total"))

    # Identify star players per team
    stars = _identify_star_players(candidates)

    # ========== GRIND: 4 PURE VOLUME LEGS ==========
    grind_candidates = sorted(
        candidates,
        key=lambda c: _score_player_for_grind(c, game_script),
        reverse=True,
    )
    grind_legs = []
    grind_players: set[str] = set()
    for cand in grind_candidates:
        if len(grind_legs) >= 4:
            break
        pid = str(cand.get("player_id", ""))
        if pid in grind_players:
            continue
        is_safe = all(
            _is_safe_pairing(cand, leg, game_script)
            for leg in grind_legs
        )
        if not is_safe:
            continue
        grind_legs.append(cand)
        grind_players.add(pid)

    # ========== MOONSHOT: 2-3 STAR TDs + 11-12 VOLUME LEGS ==========
    # First: get star player TD scorers (2-3)
    td_legs = []
    td_players: set[str] = set()

    for team, team_stars in stars.items():
        for role in ("QB", "RB1", "WR1"):  # Top 3 stars per team
            if role not in team_stars:
                continue
            star = team_stars[role]
            # Find TD market for this star
            td_cands = [c for c in candidates
                       if c.get("player_id") == star.get("player_id")
                       and str(c.get("market", "")) == "TD"]
            if td_cands:
                td_cand = td_cands[0]
                pid = str(td_cand.get("player_id", ""))
                if pid not in td_players:
                    td_legs.append(td_cand)
                    td_players.add(pid)
                    if len(td_legs) >= 3:  # Max 3 star TDs
                        break
        if len(td_legs) >= 3:
            break

    # Second: volume legs for moonshot (11-12 to reach 14-15 total)
    volume_candidates = sorted(
        candidates,
        key=lambda c: _score_player_for_moonshot_volume(c, game_script),
        reverse=True,
    )
    volume_legs = []
    volume_players = grind_players | td_players  # Exclude grind + TD players

    for cand in volume_candidates:
        if len(volume_legs) >= 11:  # 3 TDs + 11 volume = 14 legs
            break
        pid = str(cand.get("player_id", ""))
        if pid in volume_players:
            continue
        is_safe = all(
            _is_safe_pairing(cand, leg, game_script)
            for leg in (td_legs + volume_legs)
        )
        if not is_safe:
            continue
        volume_legs.append(cand)
        volume_players.add(pid)

    moonshot_legs = td_legs + volume_legs

    # ========== BUILD TICKET OBJECT ==========
    ticket = {
        "game_id": str(game_id),
        "matchup": matchup,
        "time": time,
        "game_script": game_script,
    }

    # Grind ticket (4 volume legs)
    if len(grind_legs) >= 4:
        grind_stake = 100
        grind_odds = _reference_calibrated_odds(len(grind_legs))
        ticket["grind"] = {
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
            "odds": grind_odds,
            "odds_source": "reference-calibrated estimate",
            "stake": grind_stake,
            "implied_win": _estimated_return(grind_stake, grind_odds),
        }

    # Moonshot ticket (3 star TDs + 11 volume)
    if len(moonshot_legs) >= 9:
        moonshot_stake = 25
        moonshot_odds = _reference_calibrated_odds(len(moonshot_legs))
        ticket["moonshot"] = {
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
            "odds": moonshot_odds,
            "odds_source": "reference-calibrated estimate",
            "stake": moonshot_stake,
            "implied_win": _estimated_return(moonshot_stake, moonshot_odds),
        }

    # TD Parlay (star players only)
    if len(td_legs) >= 2:
        ticket["td_parlay"] = {
            "legs": [
                {
                    "player_name": leg.get("player_name", ""),
                    "player_id": str(leg.get("player_id", "")),
                    "market": leg.get("market", ""),
                    "line": leg.get("line", ""),
                    "score": round(float(leg.get("score", 0)), 2),
                    "confidence": int(leg.get("confidence", 0)),
                }
                for leg in td_legs
            ],
            "odds": 1200 if len(td_legs) == 2 else 3000,  # 2x or 3x ~+1200/-3000
            "stake": 50,
            "implied_win": 650 if len(td_legs) == 2 else 1550,
        }

    # Return ticket if it has at least grind + moonshot
    if "grind" in ticket and "moonshot" in ticket:
        return ticket

    return None
