#!/usr/bin/env python3
import json
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: audit_snapshot.py PATH_TO_MODEL_JSON  (e.g. data/wnba-model.json, data/nba-model.json)", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    model = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []
    sequences = model.get("sequences", [])
    completed = [s for s in sequences if s.get("firstFieldGoal")]
    if len(completed) != len(sequences):
        warnings.append(f"{len(sequences) - len(completed)} games lack a first field goal")

    player_totals: dict[str, int] = defaultdict(int)
    for player in model.get("players", []):
        player_totals[str(player["teamId"])] += int(player.get("firstFieldGoals", 0))
        attempts = int(player.get("firstTeamAttempts", 0))
        makes = int(player.get("firstTeamAttemptMakes", 0))
        if makes > attempts:
            errors.append(f"{player['player']}: first-attempt makes {makes} exceed attempts {attempts}")

    sequence_totals: dict[str, int] = defaultdict(int)
    for sequence in completed:
        team_id = str(sequence["firstFieldGoal"].get("teamId"))
        sequence_totals[team_id] += 1
    for team_id, expected in sequence_totals.items():
        actual = player_totals.get(team_id, 0)
        if actual != expected:
            errors.append(f"team {team_id}: player first baskets sum to {actual}, expected {expected}")

    for team in model.get("teams", []):
        if int(team.get("tipWins", 0)) > int(team.get("games", 0)):
            errors.append(f"{team['team']}: tip wins exceed games")
        if int(team.get("games", 0)) < 5:
            warnings.append(f"{team['team']}: sample below five games")

    print(f"Opening Edge audit: {len(sequences)} games, {len(model.get('players', []))} players")
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    print("PASS" if not errors else "FAIL")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
