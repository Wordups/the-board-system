"""Tiny disk-backed JSON fetch cache.

Everything the harness pulls from the network lands under
``backend/backtest/cache/`` (gitignored), so after the first run the whole
backtest replays offline. ``offline=True`` forbids network entirely —
cache misses just return None.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

CACHE_ROOT = Path(__file__).resolve().parent / "cache"
REQUEST_TIMEOUT = 20
RETRIES = 3
BACKOFF_SECONDS = (0.5, 1.5, 4.0)


def cache_file(*parts: str) -> Path:
    path = CACHE_ROOT.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def fetch_json(url: str) -> Any:
    """GET JSON with retry/backoff (Kalshi rate-limits burst traffic)."""
    last_error: Exception | None = None
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "the-board-system-backtest/1.0",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.load(response)
        except Exception as error:  # noqa: BLE001 — retried, then re-raised
            last_error = error
    raise last_error  # type: ignore[misc]


def cached_fetch(url: str, cache_parts: tuple[str, ...], *, offline: bool = False) -> Any | None:
    """JSON from cache if present, else fetch + cache. None on any failure."""
    path = cache_file(*cache_parts)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    if offline:
        return None
    try:
        payload = fetch_json(url)
    except Exception:
        return None
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass
    return payload
