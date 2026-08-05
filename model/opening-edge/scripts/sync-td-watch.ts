// Sync the WNBA triple-double watch list from ESPN season statistics.
// Pulls PPG/RPG/APG leaders plus the double-double count, unions the athletes,
// fetches each one's season averages, and ranks by triple-double proximity
// (the weakest of the three categories, then the sum). Every number is an
// ESPN season figure — nothing is projected or hand-tuned.
//
// Usage (from model/opening-edge/):
//   node --experimental-strip-types scripts/sync-td-watch.ts [SEASON]
// Writes data/td-watch.json.
import { writeFile } from "node:fs/promises";

const season = process.argv[2] ?? String(new Date().getUTCFullYear());
const BASE = `https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/seasons/${season}/types/2`;
const TOP_PER_CATEGORY = 20;
const WATCH_SIZE = 10;

const fetchJson = async (url: string) => {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
};

const idFromRef = (ref: string) => ref.match(/athletes\/(\d+)/)?.[1] ?? null;

const leaders = await fetchJson(`${BASE}/leaders?limit=${TOP_PER_CATEGORY}`);
const wanted = new Set(["pointsPerGame", "reboundsPerGame", "assistsPerGame", "doubleDouble"]);
const athleteIds = new Set<string>();
const doubleDoubles = new Map<string, number>();
for (const category of leaders.categories ?? []) {
  if (!wanted.has(category.name)) continue;
  for (const leader of category.leaders ?? []) {
    const id = idFromRef(leader.athlete?.$ref ?? "");
    if (!id) continue;
    athleteIds.add(id);
    if (category.name === "doubleDouble") doubleDoubles.set(id, leader.value);
  }
}

const statOf = (stats: any, category: string, name: string) => {
  const group = (stats.splits?.categories ?? []).find((item: any) => item.name === category);
  const stat = (group?.stats ?? []).find((item: any) => item.name === name);
  return stat ? Number(stat.value) : null;
};

const teamAbbrCache = new Map<string, string>();
const teamAbbr = async (ref: string | undefined) => {
  if (!ref) return null;
  if (!teamAbbrCache.has(ref)) {
    try { teamAbbrCache.set(ref, (await fetchJson(ref)).abbreviation ?? null); }
    catch { teamAbbrCache.set(ref, null as unknown as string); }
  }
  return teamAbbrCache.get(ref) ?? null;
};

const players = [];
for (const id of athleteIds) {
  try {
    const [athlete, stats] = await Promise.all([
      fetchJson(`${BASE.replace("/types/2", "")}/athletes/${id}`),
      fetchJson(`${BASE}/athletes/${id}/statistics/0`),
    ]);
    const ppg = statOf(stats, "offensive", "avgPoints");
    const rpg = statOf(stats, "general", "avgRebounds");
    const apg = statOf(stats, "offensive", "avgAssists");
    if (ppg === null || rpg === null || apg === null) continue;
    players.push({
      athleteId: id,
      player: athlete.displayName,
      team: await teamAbbr(athlete.team?.$ref),
      headshot: athlete.headshot?.href ?? `https://a.espncdn.com/i/headshots/wnba/players/full/${id}.png`,
      ppg: Number(ppg.toFixed(1)),
      rpg: Number(rpg.toFixed(1)),
      apg: Number(apg.toFixed(1)),
      doubleDoubles: doubleDoubles.get(id) ?? 0,
    });
  } catch { /* skip athletes whose stats fail to resolve */ }
}

const floorOf = (player: any) => Math.min(player.ppg, player.rpg, player.apg);
const sumOf = (player: any) => player.ppg + player.rpg + player.apg;
players.sort((a, b) => floorOf(b) - floorOf(a) || sumOf(b) - sumOf(a));

const watch = players.slice(0, WATCH_SIZE).map(player => {
  const weakest = player.ppg === floorOf(player) ? "points" : player.rpg === floorOf(player) ? "rebounds" : "assists";
  return { ...player, weakest };
});

await writeFile("data/td-watch.json", JSON.stringify({
  generatedAt: new Date().toISOString(),
  season,
  source: "ESPN season averages (per game) and double-double counts",
  players: watch,
}, null, 2), "utf8");
console.log(`Wrote ${watch.length} triple-double watch players (from ${players.length} leaders) to data/td-watch.json`);
