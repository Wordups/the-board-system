from __future__ import annotations

import json
from pathlib import Path

from app.builders.kalshi_edge import enrich_board_with_kalshi
from app.builders.mlb_board_builder import build_mlb_board
from app.builders.mlb_environment import enrich_board_with_environment
from app.builders.nba_board_builder import build_nba_board
from app.builders.soccer_board_builder import build_soccer_board
from app.builders.tennis_board_builder import build_tennis_board
from app.builders.wnba_board_builder import build_wnba_board
from app.config import build_config
from app.outputs.mlb_hr_tracking import write_mlb_hr_tracking_snapshot
from app.outputs.site_exporter import export_board_to_site
from app.outputs.validator import validate_board_payload
from app.paths import build_paths


def _keep_last_good_if_empty(board: dict, *, sport_key: str, paths) -> dict:
    """Don't let a transient upstream outage wipe a populated board.

    If a fresh build has no games but the previously-exported board still has
    some, keep the previous one. This protects the live World Cup board: a local
    run can reach ESPN's WC data, but if the CI runner can't on a given hour the
    soccer build would come back empty and otherwise overwrite a good board.
    """
    if board.get("games"):
        return board
    try:
        prior = json.loads((paths.data_final / f"{sport_key}.json").read_text(encoding="utf-8"))
    except Exception:
        return board
    if prior.get("games"):
        print(
            f"[run_all] {sport_key}: fresh build empty — keeping last-good board "
            f"({len(prior['games'])} games)",
            flush=True,
        )
        return prior
    return board


def run_mlb_pipeline(project_root: Path) -> dict:
    config = build_config(project_root)
    paths = build_paths(project_root)
    board = build_mlb_board(config=config, paths=paths)
    validate_board_payload(board)
    # Display-only enrichment — adds park + wind chip per game. Runs AFTER
    # the validator so the strict schema check sees the original board, and
    # any weather-API failure leaves the env field absent (silent degrade).
    # Hard Non-Goal #1 holds: nothing here feeds backend/app/scoring/.
    enrich_board_with_environment(board)
    # Kalshi market-implied probability overlay + edge report. Additive and
    # display/report-only (REPORT_ONLY — no execution venue wired): annotates
    # ML rows with `kalshi` and adds top-level `kalshi_edge_board`. Runs AFTER
    # the validator for the same reason as the env enrichment, and degrades
    # silently (kalshi: null everywhere) on any Kalshi outage.
    enrich_board_with_kalshi(board, paths=paths)
    export_board_to_site(board=board, sport_key="mlb", paths=paths)
    write_mlb_hr_tracking_snapshot(board=board, paths=paths)
    return board


def run_nba_pipeline(project_root: Path) -> dict:
    config = build_config(project_root)
    paths = build_paths(project_root)
    board = build_nba_board(config=config, paths=paths)
    validate_board_payload(board)
    export_board_to_site(board=board, sport_key="nba", paths=paths)
    return board


def run_soccer_pipeline(project_root: Path) -> dict:
    config = build_config(project_root)
    paths = build_paths(project_root)
    board = build_soccer_board(config=config, paths=paths)
    board = _keep_last_good_if_empty(board, sport_key="soccer", paths=paths)
    validate_board_payload(board)
    export_board_to_site(board=board, sport_key="soccer", paths=paths)
    return board


def run_wnba_pipeline(project_root: Path) -> dict:
    config = build_config(project_root)
    paths = build_paths(project_root)
    board = build_wnba_board(config=config, paths=paths)
    validate_board_payload(board)
    export_board_to_site(board=board, sport_key="wnba", paths=paths)
    return board


def run_tennis_pipeline(project_root: Path) -> dict:
    config = build_config(project_root)
    paths = build_paths(project_root)
    board = build_tennis_board(config=config, paths=paths)
    validate_board_payload(board)
    export_board_to_site(board=board, sport_key="tennis", paths=paths)
    return board
