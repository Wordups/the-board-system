from __future__ import annotations

import json
from datetime import datetime, timezone


SPORT_KEYS = ("mlb", "nba", "wnba", "nfl", "soccer", "tennis")


def _read_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build_browser_snapshot(paths) -> dict:
    """Build one script-loadable snapshot so the static UI works over file://.

    GitHub Pages can fetch the individual JSON files, but local file previews
    cannot do that reliably. The script bundle is intentionally derived from
    the canonical JSON exports; it is not another data source.
    """
    sports = {}
    for sport_key in SPORT_KEYS:
        board = _read_json(paths.data_final / f"{sport_key}.json")
        if board is not None:
            sports[sport_key] = board

    snapshot = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sports": sports,
    }
    picks = _read_json(paths.data_final / "picks.json")
    if picks is not None:
        snapshot["picks"] = picks
    return snapshot


def write_browser_bundle(paths) -> None:
    payload = json.dumps(build_browser_snapshot(paths), ensure_ascii=False, separators=(",", ":"))
    # Avoid creating a closing script token if the file is ever inlined later.
    script = "window.BOARD_SNAPSHOT=" + payload.replace("</", "<\\/") + ";\n"
    for directory in (paths.pages_data, paths.frontend_data):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "snapshot.js").write_text(script, encoding="utf-8")

