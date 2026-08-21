// Unattended Opening Edge refresh (used by the twice-daily GitHub workflow).
// For each configured league: pull today's ET slate and injury report from
// ESPN, re-sync the season snapshot, run the model tests and audit, refresh
// the triple-double watch, and regenerate that league's generated section.
// Per AGENT.md a failed validation step aborts — leaving the previous board
// in place — rather than publishing a board it could not verify.
//
// The leagues run independently and in sequence: the WNBA and NBA seasons
// barely overlap, so on most days exactly one of them has games and the
// other is a clean no-op. A league with no slate today is skipped without
// touching its existing section, and a league whose refresh FAILS does not
// stop the other from running — its own section just stays as it was.
//
// Usage (from model/opening-edge/):
//   node --experimental-strip-types scripts/refresh-opening-edge.ts [LEAGUE ...]
// With no arguments every configured league is attempted.
import { spawnSync } from "node:child_process";
import { LEAGUES, defaultSeasonStart, resolveLeague, type LeagueConfig } from "../lib/leagues.ts";

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

const node = process.execPath;
const strip = "--experimental-strip-types";
const python = spawnSync("python3", ["--version"]).status === 0 ? "python3" : "python";

// A failing step throws instead of exiting the process, so one league's
// outage can't take the other league's refresh down with it.
const run = (label: string, command: string, args: string[], { optional = false } = {}) => {
  console.log(`\n== ${label}: ${command} ${args.join(" ")}`);
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.status !== 0) {
    if (optional) { console.warn(`${label} failed — continuing (optional step).`); return; }
    throw new Error(`${label} failed with status ${result.status}`);
  }
};

async function refreshLeague(league: LeagueConfig): Promise<"refreshed" | "no-slate" | "failed"> {
  // 1. Today's slate from the ESPN scoreboard.
  const scoreboard = await fetchJson(`${league.espnBase}/scoreboard?dates=${compact}`);
  const events = scoreboard.events ?? [];
  if (!events.length) {
    console.log(`No ${league.label} games on ${isoInEt} — leaving the existing board in place.`);
    return "no-slate";
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
    const report = await fetchJson(`${league.espnBase}/injuries`);
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
    console.warn(`${league.label} injury report unavailable (${error}) — continuing without lineup notes.`);
  }

  // 3. Pipeline: sync → tests → audit → rosters → watch → generate.
  try {
    run(`${league.label} season sync`, node, [strip, `scripts/sync-${league.key}.ts`, defaultSeasonStart(league, now), isoYesterdayEt]);
    run(`${league.label} model tests`, node, [strip, "--test", `tests/${league.key}-model.test.ts`]);
    run(`${league.label} snapshot audit`, python, ["scripts/audit_snapshot.py", league.modelFile]);
    run(`${league.label} roster sync`, node, [strip, "scripts/sync-rosters.ts", league.key], { optional: true });
    run(`${league.label} triple-double watch`, node, [strip, "scripts/sync-td-watch.ts", year, league.key], { optional: true });
    run(`${league.label} generate section`, node, [
      strip, "scripts/generate-section.ts",
      "--league", league.key,
      "--date", isoInEt, "--label", dateLabel, "--slate", slate,
      ...(injuries ? ["--injuries", injuries] : []),
    ]);
  } catch (error) {
    console.error(`${league.label} refresh aborted (${error}); previous ${league.label} board stays live.`);
    return "failed";
  }
  console.log(`\n${league.label} Opening Edge refreshed for ${isoInEt} (${events.length} games).`);
  return "refreshed";
}

const requested = process.argv.slice(2);
const leagues = requested.length ? requested.map(resolveLeague) : Object.values(LEAGUES);
const results = new Map<string, string>();
for (const league of leagues) {
  try {
    results.set(league.label, await refreshLeague(league));
  } catch (error) {
    console.error(`${league.label} slate lookup failed (${error}) — skipping this league.`);
    results.set(league.label, "failed");
  }
}

console.log(`\nSummary: ${[...results].map(([label, state]) => `${label} ${state}`).join(", ")}`);
// Non-zero only when every attempted league failed outright; a day where
// nobody plays is a successful no-op, not a broken refresh.
if ([...results.values()].every(state => state === "failed")) process.exit(1);
