from __future__ import annotations

from pydantic import BaseModel

from app.schemas.market_schema import MarketBoard


class TopSignal(BaseModel):
    market: str
    player_name: str
    line: str
    score: float
    confidence: int
    tier: str
    sim_prob_pct: float | None = None


class GameBoard(BaseModel):
    game_id: str
    matchup: str
    time: str
    top_signals: list[TopSignal]
    markets: MarketBoard
