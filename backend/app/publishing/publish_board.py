from __future__ import annotations

from pathlib import Path

from app.publishing.git_publisher import publish_json_updates


def publish(project_root: Path, message: str) -> None:
    publish_json_updates(project_root=project_root, message=message)
