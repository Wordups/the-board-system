from __future__ import annotations

from app.schemas.board_schema import BoardPayload


def validate_board_payload(payload: dict) -> BoardPayload:
    return BoardPayload.model_validate(payload)
