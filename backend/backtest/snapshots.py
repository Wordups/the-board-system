"""Walk historical board snapshots out of git history and extract picks.

The CI refreshes ``data/<sport>.json`` many times a day ("Refresh live
boards" commits), so git history is a per-hour archive of every board the
model ever published — including ``sim_prob_pct`` on every pick row.

For calibration we want ONE snapshot per board date, ideally captured
before games started (a later snapshot can leak in-game information into
sticky scores). Policy: the last snapshot committed at or before the
sport's pregame cutoff (ET); if the board only refreshed later that day,
fall back to the earliest snapshot of the day and flag it.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

# All dates this repo has history for (Apr-Oct) fall in EDT.
ET_OFFSET = timedelta(hours=-4)

# Last snapshot taken at/before this ET hour represents the pregame board.
PREGAME_CUTOFF_ET = {"mlb": 12, "wnba": 17, "nba": 17, "nfl": 12}

LINE_THRESHOLD = re.compile(r"(\d+)\s*\+")


def _git(repo_root: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}... failed: {result.stderr[:300]}")
    return result.stdout


def list_snapshot_commits(repo_root: str, rel_path: str) -> list[dict[str, Any]]:
    """All commits touching rel_path, oldest first: {hash, ts (aware UTC)}."""
    out = _git(repo_root, "log", "--format=%H %aI", "--", rel_path)
    commits = []
    for line in out.strip().splitlines():
        commit_hash, iso = line.split()
        ts = datetime.fromisoformat(iso).astimezone(timezone.utc)
        commits.append({"hash": commit_hash, "ts": ts})
    commits.reverse()
    return commits


def load_board(repo_root: str, commit_hash: str, rel_path: str) -> dict[str, Any] | None:
    try:
        raw = _git(repo_root, "show", f"{commit_hash}:{rel_path}")
        board = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError):
        return None
    return board if isinstance(board, dict) else None


def et_date(ts: datetime) -> str:
    return (ts.astimezone(timezone.utc) + ET_OFFSET).strftime("%Y-%m-%d")


def et_hour(ts: datetime) -> float:
    local = ts.astimezone(timezone.utc) + ET_OFFSET
    return local.hour + local.minute / 60.0


def select_daily_snapshots(
    commits: list[dict[str, Any]],
    *,
    cutoff_et_hour: int,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """One commit per ET date: last at/before the cutoff, else earliest of day.

    Adds ``date`` (ET) and ``pregame`` (False when the fallback fired).
    """
    by_date: dict[str, list[dict[str, Any]]] = {}
    for commit in commits:
        by_date.setdefault(et_date(commit["ts"]), []).append(commit)

    selected = []
    for date in sorted(by_date):
        if (start and date < start) or (end and date > end):
            continue
        day = by_date[date]  # already oldest-first
        pregame = [c for c in day if et_hour(c["ts"]) <= cutoff_et_hour]
        if pregame:
            chosen = {**pregame[-1], "pregame": True}
        else:
            chosen = {**day[0], "pregame": False}
        selected.append({**chosen, "date": date})
    return selected


def parse_line_threshold(line: str) -> int | None:
    """'HR 1+' / '2+ Hits' / '8+ K' / '15+ PTS' -> the integer threshold."""
    match = LINE_THRESHOLD.search(line or "")
    return int(match.group(1)) if match else None


def _model_prob(row: dict[str, Any]) -> float | None:
    value = row.get("sim_prob_pct")
    if value is None:
        return None
    try:
        prob = float(value) / 100.0
    except (TypeError, ValueError):
        return None
    return prob if 0.0 <= prob <= 1.0 else None


def extract_picks(board: dict[str, Any], *, sport: str, date: str) -> list[dict[str, Any]]:
    """Every prop and ML pick on the board's per-game market tables.

    Only ``games[].markets`` is read — the pinned/sim/diamond boards are
    subsets of the same rows. Rows without a simulated probability are
    skipped (early-history boards predate the sim engine).
    """
    picks: list[dict[str, Any]] = []
    for game in board.get("games") or []:
        game_id = str(game.get("game_id") or "")
        for market, rows in (game.get("markets") or {}).items():
            for row in rows or []:
                prob = _model_prob(row)
                if prob is None:
                    continue
                pick: dict[str, Any] = {
                    "sport": sport,
                    "date": date,
                    "game_id": game_id,
                    "market": market,
                    "player_id": str(row.get("player_id") or ""),
                    "player_name": row.get("player_name"),
                    "team": str(row.get("team") or "").upper(),
                    "opponent": str(row.get("opponent") or "").upper(),
                    "line": row.get("line"),
                    "threshold": None if market == "ML" else parse_line_threshold(row.get("line")),
                    "model_prob": prob,
                }
                if market == "ML":
                    kalshi = row.get("kalshi") or None
                    pick["recorded_decision"] = row.get("decision")
                    pick["recorded_implied_prob"] = (kalshi or {}).get("implied_prob")
                    pick["recorded_ticker"] = (kalshi or {}).get("ticker")
                elif pick["threshold"] is None:
                    continue  # unparseable prop line — can't grade
                picks.append(pick)
    return dedupe_picks(picks)


def dedupe_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    unique = []
    for pick in picks:
        key = (pick["date"], pick["game_id"], pick["market"], pick["player_id"], pick["line"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(pick)
    return unique
