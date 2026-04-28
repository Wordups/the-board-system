from __future__ import annotations

from pydantic import BaseModel

from app.schemas.player_schema import BoardPlayer


class MarketBoard(BaseModel):
    HR: list[BoardPlayer]
    K: list[BoardPlayer]
    Hits: list[BoardPlayer]
    TB: list[BoardPlayer]
    ML: list[BoardPlayer]
