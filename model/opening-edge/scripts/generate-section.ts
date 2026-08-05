// Generate data/opening-edge.js (the #opening section of the board) from the
// synced model snapshot, re-scored against today's actual matchups.
//
// Usage (from model/opening-edge/):
//   node --experimental-strip-types scripts/generate-section.ts \
//     --date 2026-08-05 --label "Wednesday, Aug 5" \
//     --slate "PHX@ATL=7:00 PM ET,SEA@NY=7:00 PM ET,DAL@WSH=7:30 PM ET,LA@CHI=9:00 PM ET"
//
// Every number in the output is derived from the snapshot; nothing is
// hand-tuned. Prices are model-fair lines (no sportsbook feed).
import { readFile, writeFile } from "node:fs/promises";
import { rankCandidates } from "../lib/wnba/score.ts";
import type { ModelCandidate, PlayerAggregate, TeamAggregate } from "../lib/wnba/types.ts";

const arg = (name: string, fallback = "") => {
  const index = process.argv.indexOf(`--${name}`);
  return index > -1 ? process.argv[index + 1] : fallback;
};

const date = arg("date", new Date().toISOString().slice(0, 10));
const label = arg("label", date);
const slateArg = arg("slate");
if (!slateArg) throw new Error("--slate is required, e.g. --slate \"DAL@WSH=7:30 PM ET,SEA@NY=7:00 PM ET\"");

// ESPN abbreviation -> display abbreviation used on the board.
const DISPLAY: Record<string, string> = { WSH: "WAS", NY: "NYL", GS: "GSV", LV: "LVA" };
const display = (abbr: string) => DISPLAY[abbr] ?? abbr;

const board = JSON.parse(await readFile("data/wnba-board.json", "utf8")) as {
  generatedAt: string; start: string; end: string;
  teams: TeamAggregate[]; players: PlayerAggregate[];
};
const teamByAbbr = new Map(board.teams.map(team => [team.team, team]));
const teamById = new Map(board.teams.map(team => [team.teamId, team]));

interface SlateGame { away: TeamAggregate; home: TeamAggregate; time: string }
const slate: SlateGame[] = slateArg.split(",").map(entry => {
  const [pair, time] = entry.split("=");
  const [awayAbbr, homeAbbr] = pair.trim().split("@");
  const away = teamByAbbr.get(awayAbbr.trim());
  const home = teamByAbbr.get(homeAbbr.trim());
  if (!away || !home) throw new Error(`Unknown team in slate entry "${entry}"`);
  return { away, home, time: (time ?? "").trim() };
});

const matchups: Record<string, string> = {};
for (const game of slate) {
  matchups[game.away.teamId] = game.home.teamId;
  matchups[game.home.teamId] = game.away.teamId;
}
const slateTeamIds = new Set(Object.keys(matchups));
const opponentOf = (candidate: ModelCandidate) => teamById.get(matchups[candidate.teamId])!;

const MIN_FIRST_BASKETS = 2;
const PICK_COUNT = 8;
const rate = (n: number, d: number) => (d ? n / d : 0);
const pct = (value: number, digits = 1) => Number((value * 100).toFixed(digits));

const fairAmerican = (probability: number) => {
  const p = Math.min(0.98, Math.max(0.02, probability));
  const odds = p >= 0.5 ? -Math.round((p / (1 - p)) * 100) : Math.round(((1 - p) / p) * 100);
  return odds > 0 ? `+${odds}` : String(odds);
};

const ranked = rankCandidates(board.players, board.teams, matchups)
  .filter(candidate => slateTeamIds.has(candidate.teamId))
  .filter(candidate => candidate.sample.firstFieldGoals >= MIN_FIRST_BASKETS);

// Data-derived role labels: the top first-basket share on each team is the
// team lead; high conversion on a modest share reads as a value branch.
const leadByTeam = new Map<string, string>();
for (const candidate of ranked) {
  if (!leadByTeam.has(candidate.teamId)) leadByTeam.set(candidate.teamId, candidate.athleteId);
}
const profileOf = (candidate: ModelCandidate) => {
  if (leadByTeam.get(candidate.teamId) === candidate.athleteId) return "Team lead";
  if (candidate.rates.firstAttemptMake >= 0.6 && candidate.rates.firstBasket < 0.25) return "Value";
  return "Secondary";
};

const picks = ranked.slice(0, PICK_COUNT).map(candidate => {
  const opponent = opponentOf(candidate);
  const team = teamById.get(candidate.teamId)!;
  const tipPct = pct(candidate.components.tipMatchup);
  const shotPct = pct(candidate.rates.firstAttempt, 0);
  const makePct = pct(candidate.rates.firstAttemptMake, 0);
  const signals = [
    `${candidate.sample.firstFieldGoals}/${candidate.sample.teamGames} team-first baskets`,
    `First shot in ${shotPct}% of games`,
    `Team wins ${pct(candidate.rates.teamTipWin, 0)}% of tips`,
  ];
  const cautions: string[] = [];
  if (candidate.sample.teamGames < 15) cautions.push(`Small sample: ${candidate.sample.teamGames} games`);
  if (candidate.components.tipMatchup < 0.5) cautions.push(`Tip disadvantage vs ${display(opponent.team)}`);
  if (candidate.rates.firstAttemptMake < 0.5) cautions.push(`Converts ${makePct}% of first attempts`);
  if (candidate.sample.firstAttempts < 5) cautions.push(`Only ${candidate.sample.firstAttempts} tracked first attempts`);
  return {
    player: candidate.player,
    headshot: `https://a.espncdn.com/i/headshots/wnba/players/full/${candidate.athleteId}.png`,
    team: display(team.team),
    opp: display(opponent.team),
    profile: profileOf(candidate),
    score: candidate.edgeScore,
    odds: fairAmerican(candidate.rates.firstBasket),
    fb: `${candidate.sample.firstFieldGoals} / ${candidate.sample.teamGames}`,
    tip: tipPct,
    shot: shotPct,
    make: makePct,
    script: `${display(team.team)} wins ${pct(candidate.rates.teamTipWin, 0)}% of tips and scores first in ${pct(candidate.rates.teamScoresFirst, 0)}% of games; ${candidate.player.split(" ").slice(-1)[0]} takes the first shot in ${shotPct}% and converts ${makePct}% of those attempts.`,
    signals,
    cautions,
  };
});

const games = slate.map(game => {
  const homeTipRate = rate(game.home.tipWins, game.home.games);
  const awayTipRate = rate(game.away.tipWins, game.away.games);
  const split = homeTipRate + awayTipRate;
  const homeTip = split ? pct(homeTipRate / split) : 50;
  const homeFirst = rate(game.home.scoredFirstFieldGoal, game.home.games);
  const awayFirst = rate(game.away.scoredFirstFieldGoal, game.away.games);
  const gapPp = pct(homeFirst - awayFirst);
  const leader = gapPp >= 0 ? display(game.home.team) : display(game.away.team);
  return {
    away: display(game.away.team),
    home: display(game.home.team),
    time: game.time,
    homeTip,
    edge: Math.abs(gapPp) < 3 ? "Even lean" : `${leader} +${Math.abs(gapPp)}pp`,
    note: `${display(game.home.team)} scores first in ${pct(homeFirst, 0)}% of games, ${display(game.away.team)} in ${pct(awayFirst, 0)}%.`,
  };
});

const output = {
  league: "WNBA",
  date,
  dateLabel: label,
  updated: `synced ${board.start} → ${board.end}`,
  source: "ESPN play-by-play · first field goal market · prices are model-fair lines, no sportsbook feed",
  headshotBase: "https://a.espncdn.com/i/headshots/wnba/players/full/",
  weights: [
    [30, "Player FB share"], [20, "Team opening profile"], [15, "First-shot involvement"],
    [15, "Tip matchup"], [10, "Role / availability"], [5, "H2H"], [5, "Price"],
  ],
  picks,
  games,
};

const banner = `// Opening Edge — WNBA first-basket model section (#opening).
// GENERATED by model/opening-edge/scripts/generate-section.ts from the synced
// snapshot (${board.generatedAt}); do not hand-edit. Regenerate with a slate:
//   cd model/opening-edge && node --experimental-strip-types scripts/generate-section.ts \\
//     --date YYYY-MM-DD --label "Day, Mon D" --slate "AWY@HOM=7:00 PM ET,..."
`;
await writeFile("../../data/opening-edge.js", `${banner}window.OPENING_EDGE = ${JSON.stringify(output, null, 2)};\n`, "utf8");
console.log(`Wrote ${picks.length} picks, ${games.length} games for ${date} to data/opening-edge.js`);
