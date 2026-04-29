from __future__ import annotations

from pathlib import Path

from app.builders.mlb_board_builder import build_mlb_board
from app.builders.nba_board_builder import build_nba_board
from app.builders.soccer_board_builder import build_soccer_board
from app.builders.tennis_board_builder import build_tennis_board
from app.builders.wnba_board_builder import build_wnba_board
from app.config import build_config
from app.outputs.site_exporter import export_board_to_site
from app.outputs.validator import validate_board_payload
from app.paths import build_paths


def run_mlb_pipeline(project_root: Path) -> dict:
    config = build_config(project_root)
    paths = build_paths(project_root)
    board = build_mlb_board(config=config, paths=paths)
    validate_board_payload(board)
    export_board_to_site(board=board, sport_key="mlb", paths=paths)
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
