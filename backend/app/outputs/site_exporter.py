from __future__ import annotations

import shutil

from app.outputs.json_writer import write_json


def export_board_to_site(*, board: dict, sport_key: str, paths) -> None:
    final_path = paths.data_final / f"{sport_key}.json"
    write_json(final_path, board)
    paths.frontend_data.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final_path, paths.frontend_data / f"{sport_key}.json")
