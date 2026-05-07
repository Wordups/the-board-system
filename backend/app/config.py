from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    project_root: Path
    timezone_name: str = "America/New_York"
    top_market_limit: int = 10
    top_signals_per_game: int = 3
    hr_core_count: int = 3
    hr_watch_count: int = 7
    copied_sports: tuple[str, ...] = field(default_factory=lambda: ("mlb",))
    parlay_leg_sizes: tuple[int, ...] = field(default_factory=lambda: (2, 3, 4, 6))
    parlay_max_same_team: int = 2
    parlay_max_same_game: int = 2


def build_config(project_root: Path) -> AppConfig:
    return AppConfig(project_root=project_root)
