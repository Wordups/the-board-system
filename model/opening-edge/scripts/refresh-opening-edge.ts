// Unattended Opening Edge refresh (used by the twice-daily GitHub workflow).
// Pulls today's ET slate and injury report from ESPN, re-syncs the season
// snapshot, runs the model tests and audit, refreshes the triple-double
// watch, and regenerates data/opening-edge.js. Per AGENT.md it aborts —
// leaving the previous board in place — if any validation step fails.
//
// Usage (from model/opening-edge/): node --experimental-strip-types scripts/refresh-opening-edge.ts
import { spawnSync } from "node:child_process";

const ET = "America/New_York";
const now = new Date();
const fmt = (options: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat("en-US", { timeZone: ET, ...options }).format(now);
const isoInEt = new Intl.DateTimeFormat("en-CA", { timeZone: ET, year: "numeric", month: "2-digit", day: "2-digit" }).format(now); // YYYY-MM-DD
const compact = isoInEt.replaceAll("-", "");
const year = isoInEt.slice(0, 4);
const yesterday = new Date(now.getTime() - 24 * 60 * 60 * 1000);
const isoYesterdayEt = new Intl.DateTimeFormat("en-CA", { timeZone: ET, year: "numeric", month: "2-digit", day: "2-digit" }).format(yesterday);
const dateLabel = `${fmt({ weekday: "long" })}, ${fmt({ month: "short" })} ${fmt({ day: "numeric" })}`;

const fetchJson = async (url: string) => {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
};

// 1. Today's slate from the ESPN scoreboard.
const scoreboard = await fetchJson(`https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard?dates=${compact}`);
const events = scoreboard.events ?? [];
if (!events.length) {
  console.log(`No WNBA games on ${isoInEt} — leaving the existing board in place.`);
  process.exit(0);
}
const slate = events.map((event: any) => {
  const competitors = event.competitions[0].competitors;
  const away = competitors.find((item: any) => item.homeAway === "away").team.abbreviation;
  const home = competitors.find((item: any) => item.homeAway === "home").team.abbreviation;
  const time = new Intl.DateTimeFormat("en-US", { timeZone: ET, hour: "numeric", minute: "2-digit" }).format(new Date(event.date));
  return `${away}@${home}=${time} ET`;
}).join(",");

// 2. Injury report, keyed by ESPN team abbreviation.
let injuries = "";
try {
  const report = await fetchJson("https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries");
  const byTeam = new Map<string, string[]>();
  for (const team of report.injuries ?? []) {
    for (const injury of team.injuries ?? []) {
      const abbr = injury.athlete?.team?.abbreviation;
      const name = injury.athlete?.displayName;
      const status = injury.status;
      if (!abbr || !name || !status) continue;
      if (!byTeam.has(abbr)) byTeam.set(abbr, []);
      byTeam.get(abbr)!.push(`${name} ${status}`);
    }
  }
  injuries = [...byTeam.entries()].map(([abbr, list]) => `${abbr}:${list.join(",")}`).join(";");
} catch (error) {
  console.warn(`Injury report unavailable (${error}) — continuing without lineup notes.`);
}

// 3. Pipeline: sync → tests → audit → TD watch → generate. Any failure aborts.
const run = (label: string, command: string, args: string[], { optional = false } = {}) => {
  console.log(`\n== ${label}: ${command} ${args.join(" ")}`);
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.status !== 0) {
    if (optional) { console.warn(`${label} failed — continuing (optional step).`); return; }
    console.error(`${label} failed with status ${result.status}; aborting refresh, previous board stays live.`);
    process.exit(1);
  }
};
const node = process.execPath;
const strip = "--experimental-strip-types";
const python = spawnSync("python3", ["--version"]).status === 0 ? "python3" : "python";

run("Season sync", node, [strip, "scripts/sync-wnba.ts", `${year}-05-01`, isoYesterdayEt]);
run("Model tests", node, [strip, "--test", "tests/wnba-model.test.ts"]);
run("Snapshot audit", python, ["scripts/audit_snapshot.py", "data/wnba-model.json"]);
run("Roster sync", node, [strip, "scripts/sync-rosters.ts"], { optional: true });
run("Triple-double watch", node, [strip, "scripts/sync-td-watch.ts", year], { optional: true });
run("Generate section", node, [
  strip, "scripts/generate-section.ts",
  "--date", isoInEt, "--label", dateLabel, "--slate", slate,
  ...(injuries ? ["--injuries", injuries] : []),
]);
console.log(`\nOpening Edge refreshed for ${isoInEt} (${events.length} games).`);
