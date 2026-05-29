from __future__ import annotations

from app.outputs.diamond_validator import validate_diamond_field
from app.schemas.board_schema import BoardPayload


def validate_board_payload(payload: dict) -> BoardPayload:
    model = BoardPayload.model_validate(payload)
    validate_diamond_field(payload)
    return model
