from __future__ import annotations


def slugify(value: str) -> str:
    return value.lower().replace(" ", "-").replace(".", "").replace("@", "at")
