from __future__ import annotations

from app.schemas.board_schema import BoardPayload


def test_board_schema_accepts_contract_shape():
    payload = {
        "sport": "MLB",
        "date": "2026-04-28",
        "last_updated": "10:45 AM ET",
        "pinned_board": {
            "title": "HR Top 10",
            "market": "HR",
            "players": [],
        },
        "games": [
            {
                "game_id": "nyy-bos-2026-04-28",
                "matchup": "NYY @ BOS",
                "time": "7:05 PM ET",
                "top_signals": [],
                "markets": {"HR": [], "K": [], "Hits": [], "TB": [], "ML": []},
            }
        ],
    }
    parsed = BoardPayload.model_validate(payload)
    assert parsed.sport == "MLB"
