from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    project_root: Path
    timezone_name: str = "America/New_York"
    top_market_limit: int = 10
    top_signals_per_game: int = 3
    copied_sports: tuple[str, ...] = field(default_factory=lambda: ("mlb",))


def build_config(project_root: Path) -> AppConfig:
    return AppConfig(project_root=project_root)
