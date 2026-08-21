// Sync current rosters from ESPN so the board never presents a stale team
// label as current (AGENT.md: flag trades and stale team labels).
// Writes data/<league>-rosters.json mapping athleteId -> current team.
//
// Usage (from model/opening-edge/):
//   node --experimental-strip-types scripts/sync-rosters.ts [LEAGUE]
import { readFile, writeFile } from "node:fs/promises";
import { resolveLeague } from "../lib/leagues.ts";

const league = resolveLeague(process.argv[2]);
const board = JSON.parse(await readFile(league.boardFile, "utf8")) as {
  teams: Array<{ teamId: string; team: string }>;
};

const athletes: Record<string, { team: string; name: string }> = {};
for (const team of board.teams) {
  const response = await fetch(`${league.espnBase}/teams/${team.teamId}/roster`);
  if (!response.ok) throw new Error(`Roster ${team.team} (${team.teamId}): ${response.status}`);
  const json = await response.json() as { athletes?: Array<{ id: string; displayName?: string }> };
  for (const athlete of json.athletes ?? []) {
    athletes[String(athlete.id)] = { team: team.team, name: athlete.displayName ?? String(athlete.id) };
  }
}

await writeFile(league.rosterFile, JSON.stringify({
  generatedAt: new Date().toISOString(),
  league: league.key,
  source: "ESPN team rosters",
  athletes,
}, null, 2), "utf8");
console.log(`${league.label}: wrote ${Object.keys(athletes).length} rostered athletes across ${board.teams.length} teams to ${league.rosterFile}`);
