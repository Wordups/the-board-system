// Sync current WNBA rosters from ESPN so the board never presents a stale
// team label as current (AGENT.md: flag trades and stale team labels).
// Writes data/rosters.json mapping athleteId -> current team abbreviation.
//
// Usage (from model/opening-edge/): node --experimental-strip-types scripts/sync-rosters.ts
import { readFile, writeFile } from "node:fs/promises";

const board = JSON.parse(await readFile("data/wnba-board.json", "utf8")) as {
  teams: Array<{ teamId: string; team: string }>;
};

const athletes: Record<string, { team: string; name: string }> = {};
for (const team of board.teams) {
  const response = await fetch(`https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/${team.teamId}/roster`);
  if (!response.ok) throw new Error(`Roster ${team.team} (${team.teamId}): ${response.status}`);
  const json = await response.json() as { athletes?: Array<{ id: string; displayName?: string }> };
  for (const athlete of json.athletes ?? []) {
    athletes[String(athlete.id)] = { team: team.team, name: athlete.displayName ?? String(athlete.id) };
  }
}

await writeFile("data/rosters.json", JSON.stringify({
  generatedAt: new Date().toISOString(),
  source: "ESPN team rosters",
  athletes,
}, null, 2), "utf8");
console.log(`Wrote ${Object.keys(athletes).length} rostered athletes across ${board.teams.length} teams to data/rosters.json`);
