#!/usr/bin/env python
"""Generate NFL parlays from live FanDuel props.

Usage:
    python generate_parlays_from_fanduel.py --games "CAR@ATL" "PHI@TEN" "MIA@SF" "IND@KC"

This script demonstrates the end-to-end capability to:
1. Scrape real player props from FanDuel
2. Feed them to the parlay generator
3. Output real parlays with actual player names and odds
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.builders.nfl_parlays_builder import build_nfl_parlays_board_from_fanduel
from app.outputs.json_writer import write_json

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def parse_games(game_strings: list[str]) -> list[dict[str, str]]:
    """Parse game strings like 'CAR@ATL' into game dicts."""
    games = []
    for game_str in game_strings:
        parts = game_str.split("@")
        if len(parts) != 2:
            logger.warning(f"Invalid game format: {game_str}, skipping")
            continue
        away, home = [p.strip().upper() for p in parts]
        # FanDuel URLs use lowercase and full team names
        game_id = f"{away}_{home}"
        matchup = f"{away} @ {home}"
        games.append({
            "game_id": game_id,
            "matchup": matchup,
        })
    return games


def main():
    parser = argparse.ArgumentParser(
        description="Generate NFL parlays from FanDuel props"
    )
    parser.add_argument(
        "games",
        nargs="+",
        help="Game matchups in format 'AWAY@HOME' (e.g., CAR@ATL PHI@TEN)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="frontend/data/nfl_parlays.json",
        help="Output file path (default: frontend/data/nfl_parlays.json)",
    )

    args = parser.parse_args()

    games = parse_games(args.games)
    if not games:
        logger.error("No valid games parsed")
        sys.exit(1)

    logger.info(f"Generating parlays for {len(games)} games:")
    for game in games:
        logger.info(f"  - {game['matchup']}")

    logger.info("\nFetching FanDuel props...")
    try:
        board = build_nfl_parlays_board_from_fanduel(games=games)
    except Exception as e:
        logger.error(f"Error building parlay board: {e}")
        sys.exit(1)

    logger.info(f"\nGenerated {len(board['parlays'])} game tickets")
    logger.info(f"Generated {len(board['td_parlays'])} TD parlays")

    # Save output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        write_json(output_path, board)
        logger.info(f"\n✓ Saved to {output_path}")
        logger.info(f"\nSample output:")
        if board["parlays"]:
            sample = board["parlays"][0]
            logger.info(f"  Game: {sample['matchup']}")
            logger.info(f"  Grind: {sample['grind']['legs'].__len__()} legs, +{sample['grind']['odds']}")
            logger.info(f"  Moonshot: {sample['moonshot']['legs'].__len__()} legs, +{sample['moonshot']['odds']}")
    except Exception as e:
        logger.error(f"Error writing output: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
