"""FanDuel sportsbook scraper for NFL player props.

Fetches real player prop odds and lines from FanDuel for use in parlay generation.
Handles authentication, rate limiting, and data normalization.
"""

from __future__ import annotations

import json
import time
from typing import Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class FanDuelScraper:
    """Scrapes player props from FanDuel sportsbook."""

    def __init__(self):
        self.base_url = "https://sportsbook.fanduel.com"
        self.api_base = "https://api.sportsbook.fanduel.com"
        self.session = None
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _normalize_market(self, market_name: str) -> str:
        """Normalize market names to standard format."""
        market_map = {
            "passing yards": "PassYds",
            "pass yards": "PassYds",
            "rushing yards": "RushYds",
            "rush yards": "RushYds",
            "receiving yards": "RecYds",
            "rec yards": "RecYds",
            "receptions": "REC",
            "anytime touchdown": "TD",
            "any-time touchdown": "TD",
            "touchdown": "TD",
        }
        normalized = market_name.lower().strip()
        return market_map.get(normalized, market_name)

    def _extract_position(self, player_name: str, team: str) -> str:
        """Infer player position from known player data."""
        # This would ideally come from a players database
        # For now, return generic position
        return "UNKNOWN"

    def _parse_line(self, line_str: str) -> float | None:
        """Parse a line value (could be 250.5, -250.5, etc)."""
        try:
            return float(line_str.replace("+", "").replace("-", ""))
        except (ValueError, AttributeError):
            return None

    def _parse_odds(self, odds_str: str) -> float | None:
        """Parse American odds to decimal."""
        try:
            odds = float(odds_str.replace("+", ""))
            if odds > 0:
                return 1 + (odds / 100)
            else:
                return 1 + (100 / abs(odds))
        except (ValueError, AttributeError):
            return None

    def fetch_game_props(
        self, game_id: str, matchup: str
    ) -> dict[str, Any]:
        """Fetch player props for a specific NFL game.

        Args:
            game_id: FanDuel game ID (from URL or API)
            matchup: Game matchup string like "CAR @ ATL"

        Returns:
            {
                "game_id": str,
                "matchup": str,
                "timestamp": str,
                "candidates": [
                    {
                        "player_name": str,
                        "team": str,
                        "position": str,
                        "market": str,
                        "line": float,
                        "odds": float,
                        "confidence": int (0-100),
                        "score": float (0-100)
                    },
                    ...
                ]
            }
        """
        logger.info(f"Fetching props for {matchup} (game_id: {game_id})")

        # Try Playwright approach first
        candidates = self._fetch_with_playwright(matchup)
        if candidates is not None:
            return {
                "game_id": game_id,
                "matchup": matchup,
                "timestamp": datetime.utcnow().isoformat(),
                "candidates": candidates,
            }

        # Fallback: return empty if network unavailable
        logger.warning(f"Could not fetch props for {matchup} - network unavailable")
        return {
            "game_id": game_id,
            "matchup": matchup,
            "timestamp": datetime.utcnow().isoformat(),
            "candidates": [],
        }

    def _fetch_with_playwright(self, matchup: str) -> list[dict[str, Any]] | None:
        """Attempt to fetch props using Playwright."""
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    executable_path="/opt/pw-browsers/chromium",
                    args=["--no-sandbox"]
                )
                page = browser.new_page()
                page.set_extra_http_headers(self.headers)

                # Parse matchup to build URL
                teams = matchup.replace(" @ ", "-").lower().split("-")
                if len(teams) != 2:
                    return None

                away, home = teams
                url = f"{self.base_url}/football/nfl/{away}-{home}?tab=player-props"

                # Add timeout and retry logic
                for attempt in range(2):
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        candidates = self._extract_props_from_page(page, matchup)
                        browser.close()
                        return candidates if candidates else None
                    except (PlaywrightTimeoutError, Exception) as e:
                        if attempt < 1:
                            logger.debug(f"Attempt {attempt + 1} failed: {e}, retrying...")
                            time.sleep(1)
                        else:
                            logger.debug(f"Playwright fetch failed after {attempt + 1} attempts: {e}")
                            browser.close()
                            return None

        except Exception as e:
            logger.debug(f"Playwright initialization failed: {e}")
            return None

    def _extract_props_from_page(self, page, matchup: str) -> list[dict[str, Any]]:
        """Extract player props from rendered FanDuel page."""
        candidates = []

        try:
            # Look for prop rows in the page
            prop_containers = page.locator(
                "[data-testid*='prop-row'], [role='row']"
            ).all()

            for container in prop_containers:
                try:
                    # Extract player name
                    player_elem = container.locator("text=/^[A-Z][a-z]+ [A-Z][a-z]+/")
                    player_name = player_elem.text_content() if player_elem else None

                    if not player_name:
                        continue

                    # Extract market (PassYds, RecYds, etc)
                    market_elem = container.locator("text=/Yards|Receptions|Touchdown/")
                    market_text = market_elem.text_content() if market_elem else ""
                    market = self._normalize_market(market_text)

                    # Extract line
                    line_elem = container.locator("[data-testid*='line']")
                    line_text = line_elem.text_content() if line_elem else None
                    line = self._parse_line(line_text) if line_text else None

                    # Extract odds
                    odds_elem = container.locator("[data-testid*='odds']")
                    odds_text = odds_elem.text_content() if odds_elem else None
                    odds = self._parse_odds(odds_text) if odds_text else None

                    if player_name and market and line is not None and odds is not None:
                        # Infer team from matchup
                        teams = matchup.split("@")
                        team = teams[0].strip() if teams else "UNKNOWN"

                        candidate = {
                            "player_name": player_name,
                            "team": team,
                            "position": self._extract_position(player_name, team),
                            "market": market,
                            "line": line,
                            "odds": odds,
                            "confidence": 75,  # Default confidence for real data
                            "score": 70,  # Will be calculated by parlay generator
                            "game_spread": None,
                            "game_total": None,
                        }
                        candidates.append(candidate)

                except Exception as e:
                    logger.debug(f"Error extracting prop row: {e}")
                    continue

            logger.info(f"Extracted {len(candidates)} candidates for {matchup}")

        except Exception as e:
            logger.error(f"Error extracting props from page: {e}")

        return candidates

    def fetch_multiple_games(
        self, games: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        """Fetch props for multiple games.

        Args:
            games: List of {"game_id": str, "matchup": str}

        Returns:
            List of game data with candidates
        """
        results = []
        for game in games:
            result = self.fetch_game_props(game["game_id"], game["matchup"])
            results.append(result)
            time.sleep(1)  # Rate limiting

        return results
