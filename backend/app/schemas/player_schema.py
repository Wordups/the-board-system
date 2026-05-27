from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any


class RawPlayerMarketInput(BaseModel):
    player_id: str
    player_name: str
    team: str
    opponent: str
    game_id: str
    market: str
    line: str
    stat_value: float = Field(ge=0.0, le=1.0)
    baseline: float = Field(ge=0.0, le=1.0)
    trend: float = Field(ge=0.0, le=1.0)
    matchup: float = Field(ge=0.0, le=1.0)
    recent_form: float = Field(ge=0.0, le=1.0)
    extra: dict[str, Any] = Field(default_factory=dict)


class BoardPlayer(BaseModel):
    player_id: str
    player_name: str
    team: str
    opponent: str
    line: str
    score: float
    confidence: int = Field(ge=1, le=99)
    tier: str
    reason: str
    # Phase 12: simulated probability of clearing the line (0-100). Nullable
    # during parallel rollout. book_odds is reserved for Phase 14 odds ingestion;
    # edge_pct then slots in as (sim_prob - book_implied) / book_implied.
    sim_prob_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    book_odds: float | None = None
