from __future__ import annotations

from pydantic import BaseModel

from app.schemas.game_schema import GameBoard
from app.schemas.player_schema import BoardPlayer


class PinnedBoard(BaseModel):
    title: str
    market: str
    players: list[BoardPlayer]


class CalibrationMeta(BaseModel):
    threshold_pp: float
    mode: str
    held_count: int
    held_for_calibration: list[dict]


class BoardPayload(BaseModel):
    sport: str
    date: str
    last_updated: str
    calibration: CalibrationMeta | None = None
    pinned_board: PinnedBoard
    consistency_board: PinnedBoard | None = None
    diamond: dict | None = None  # Diamond of the Day (view over scored rows)
    games: list[GameBoard]
