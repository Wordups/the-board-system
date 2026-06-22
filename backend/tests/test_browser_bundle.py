from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.outputs.browser_bundle import build_browser_snapshot, write_browser_bundle


def test_browser_bundle_mirrors_canonical_exports(tmp_path):
    data_final = tmp_path / "final"
    pages_data = tmp_path / "pages"
    frontend_data = tmp_path / "frontend"
    data_final.mkdir()
    board = {"sport": "MLB", "date": "2026-06-22", "games": []}
    (data_final / "mlb.json").write_text(json.dumps(board), encoding="utf-8")
    paths = SimpleNamespace(
        data_final=data_final,
        pages_data=pages_data,
        frontend_data=frontend_data,
    )

    snapshot = build_browser_snapshot(paths)
    assert snapshot["schema_version"] == 2
    assert snapshot["sports"]["mlb"] == board

    write_browser_bundle(paths)
    for path in (pages_data / "snapshot.js", frontend_data / "snapshot.js"):
        script = path.read_text(encoding="utf-8")
        assert script.startswith("window.BOARD_SNAPSHOT=")
        assert '"sport":"MLB"' in script
