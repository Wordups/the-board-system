from __future__ import annotations


def empty_markets() -> dict[str, list]:
    return {
        "HR": [],
        "K": [],
        "Hits": [],
        "TB": [],
        "ML": [],
    }


def empty_markets_for(markets: list[str]) -> dict[str, list]:
    return {market: [] for market in markets}
