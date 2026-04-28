from __future__ import annotations

from pydantic import BaseModel

from app.schemas.game_schema import GameBoard
from app.schemas.player_schema import BoardPlayer


class PinnedBoard(BaseModel):
    title: str
    market: str
    players: list[BoardPlayer]


class BoardPayload(BaseModel):
    sport: str
    date: str
    last_updated: str
    pinned_board: PinnedBoard
    games: list[GameBoard]
