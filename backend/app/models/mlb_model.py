from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.player_schema import RawPlayerMarketInput


@dataclass(slots=True)
class MlbPlayCandidate:
    player_id: str
    player_name: str
    team: str
    opponent: str
    game_id: str
    market: str
    line: str
    stat_value: float
    baseline: float
    trend: float
    matchup: float
    recent_form: float
    score: float = 0.0
    confidence: int = 0
    tier: str = "PASS"
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def normalize_mlb_inputs(raw_game: dict[str, Any]) -> list[MlbPlayCandidate]:
    return [
        MlbPlayCandidate(**RawPlayerMarketInput(**player).model_dump())
        for player in raw_game["players"]
    ]
