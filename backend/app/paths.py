from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppPaths:
    project_root: Path
    backend_root: Path
    frontend_root: Path
    data_raw: Path
    data_processed: Path
    data_final: Path
    frontend_data: Path


def build_paths(project_root: Path) -> AppPaths:
    backend_root = project_root / "backend"
    frontend_root = project_root / "frontend"
    return AppPaths(
        project_root=project_root,
        backend_root=backend_root,
        frontend_root=frontend_root,
        data_raw=backend_root / "data_raw",
        data_processed=backend_root / "data_processed",
        data_final=backend_root / "data_final",
        frontend_data=frontend_root / "data",
    )
