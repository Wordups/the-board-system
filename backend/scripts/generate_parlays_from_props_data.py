#!/usr/bin/env python
"""Generate NFL parlays from manually-provided props data.

Usage:
    python generate_parlays_from_props_data.py --data props.json --output parlays.json

This is useful for:
1. Testing the parlay generator with specific props
2. Using props data from other sources (manual, API, etc)
3. Demonstrating the full pipeline with real player names and odds
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.sim.nfl_parlays import build_same_game_parlays_for_game
from app.outputs.json_writer import write_json
from app.utils.dates import timestamp_et


def build_parlays_from_props(props_by_game: dict[str, Any]) -> dict[str, Any]:
    """Build parlay board from raw props data.

    Args:
        props_by_game: {
            "game_id": {
                "matchup": "CAR @ ATL",
                "time": "1:00 PM",
                "candidates": [player props...]
            }
        }
    """
    parlays = []
    td_parlays = []

    for game_id, game_data in props_by_game.items():
        try:
            ticket = build_same_game_parlays_for_game(
                game_id=game_id,
                matchup=game_data["matchup"],
                time=game_data.get("time", ""),
                candidates=game_data.get("candidates", []),
            )

            if ticket:
                parlays.append(ticket)
                # Extract TD parlay if present
                if "td_parlay" in ticket:
                    td_parlays.append({
                        "game_id": ticket["game_id"],
                        "matchup": ticket["matchup"],
                        "time": ticket.get("time", ""),
                        "game_script": ticket["game_script"],
                        "td_parlay": ticket["td_parlay"],
                    })
        except Exception as e:
            print(f"Error processing {game_id}: {e}")
            continue

    return {
        "sport": "NFL",
        "date": timestamp_et().split("T")[0],
        "last_updated": timestamp_et(),
        "parlays": parlays,
        "td_parlays": td_parlays,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Generate parlays from props data"
    )
    parser.add_argument(
        "--data",
        "-d",
        required=True,
        help="Props data JSON file",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="frontend/data/nfl_parlays.json",
        help="Output file (default: frontend/data/nfl_parlays.json)",
    )

    args = parser.parse_args()

    # Load props data
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"Error: {data_path} not found")
        sys.exit(1)

    try:
        with open(data_path) as f:
            props_data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        sys.exit(1)

    # Build parlays
    board = build_parlays_from_props(props_data)

    # Save output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        write_json(output_path, board)
        print(f"✓ Generated {len(board['parlays'])} tickets with {len(board['td_parlays'])} TD parlays")
        print(f"✓ Saved to {output_path}")
    except Exception as e:
        print(f"Error writing output: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
