from __future__ import annotations

from pydantic import RootModel

from app.schemas.player_schema import BoardPlayer


class MarketBoard(RootModel[dict[str, list[BoardPlayer]]]):
    pass
