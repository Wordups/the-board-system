// NBA adapter validation. AGENT.md's new-league rule: before enabling
// another league, prove the adapter maps that feed into the canonical
// opening-event schema (references/event-schema.md) against a known-game
// fixture that exercises tip, first attempt, the miss branch, and the first
// score -- including the first-points-vs-first-field-goal split a free
// throw creates.
//
// The NBA rides the same ESPN basketball play-by-play shape as the WNBA, so
// this file pins that claim to a fixture rather than asserting it in prose.
import assert from "node:assert/strict";
import test from "node:test";
import { LEAGUES, defaultSeasonStart, displayAbbr, resolveLeague } from "../lib/leagues.ts";
import { aggregateSequences } from "../lib/wnba/aggregate.ts";
import { normalizeSummary } from "../lib/wnba/espn.ts";
import { extractOpeningSequence } from "../lib/wnba/opening-sequence.ts";
import { scoreCandidate } from "../lib/wnba/score.ts";
import type { GameSummary } from "../lib/wnba/types.ts";

// Known-game shape: Brooklyn at Denver. Denver wins the tip and Murray
// misses the opening look (miss branch); Brooklyn scores the game's first
// POINTS at the line; Denver scores the game's first FIELD GOAL on Porter's
// assisted three; Brooklyn's own first field goal lands after that.
const game: GameSummary = {
  gameId: "401705000",
  date: "2026-01-29T02:00Z",
  name: "Brooklyn Nets at Denver Nuggets",
  teams: [
    { id: "17", abbreviation: "BKN", displayName: "Brooklyn Nets" },
    { id: "7", abbreviation: "DEN", displayName: "Denver Nuggets" },
  ],
  athletes: {
    "3112335": { id: "3112335", name: "Nikola Jokic" },
    "3936299": { id: "3936299", name: "Jamal Murray" },
    "4066211": { id: "4066211", name: "Michael Porter Jr." },
    "4397424": { id: "4397424", name: "Nic Claxton" },
    "4432816": { id: "4432816", name: "Cam Thomas" },
  },
  plays: [
    { id: "4", sequenceNumber: "4", type: { text: "Jumpball" }, text: "Nikola Jokic vs. Nic Claxton (Jamal Murray gains possession)", period: { number: 1 }, clock: { displayValue: "12:00" }, team: { id: "7" }, participants: [{ athlete: { id: "3112335" } }, { athlete: { id: "4397424" } }, { athlete: { id: "3936299" } }] },
    { id: "7", sequenceNumber: "7", type: { text: "Pullup Jump Shot" }, text: "Jamal Murray misses 17-foot pullup jump shot", period: { number: 1 }, clock: { displayValue: "11:42" }, team: { id: "7" }, participants: [{ athlete: { id: "3936299" } }], shootingPlay: true, scoringPlay: false, scoreValue: 0, pointsAttempted: 2 },
    { id: "9", sequenceNumber: "9", type: { text: "Personal Foul" }, text: "Michael Porter Jr. personal foul", period: { number: 1 }, clock: { displayValue: "11:30" }, team: { id: "7" }, participants: [{ athlete: { id: "4066211" } }] },
    { id: "11", sequenceNumber: "11", type: { text: "Free Throw - 1 of 2" }, text: "Cam Thomas makes free throw 1 of 2", period: { number: 1 }, clock: { displayValue: "11:30" }, team: { id: "17" }, participants: [{ athlete: { id: "4432816" } }], scoringPlay: true, scoreValue: 1, pointsAttempted: 1 },
    { id: "14", sequenceNumber: "14", type: { text: "Layup Shot" }, text: "Nic Claxton misses layup", period: { number: 1 }, clock: { displayValue: "11:05" }, team: { id: "17" }, participants: [{ athlete: { id: "4397424" } }], shootingPlay: true, scoringPlay: false, scoreValue: 0, pointsAttempted: 2 },
    { id: "17", sequenceNumber: "17", type: { text: "Three Point Jump Shot" }, text: "Michael Porter Jr. makes 26-foot three point jumper (Nikola Jokic assists)", period: { number: 1 }, clock: { displayValue: "10:47" }, team: { id: "7" }, participants: [{ athlete: { id: "4066211" } }, { athlete: { id: "3112335" } }], shootingPlay: true, scoringPlay: true, scoreValue: 3, pointsAttempted: 3 },
    { id: "20", sequenceNumber: "20", type: { text: "Driving Layup Shot" }, text: "Cam Thomas makes driving layup", period: { number: 1 }, clock: { displayValue: "10:21" }, team: { id: "17" }, participants: [{ athlete: { id: "4432816" } }], shootingPlay: true, scoringPlay: true, scoreValue: 2, pointsAttempted: 2 },
  ],
};

test("NBA adapter maps tip, first attempt and the miss branch", () => {
  const sequence = extractOpeningSequence(game);
  assert.equal(sequence.tip.winningTeamId, "7");
  assert.equal(sequence.tip.possessionPlayerName, "Jamal Murray");
  assert.equal(sequence.firstAttempt?.athleteName, "Jamal Murray");
  assert.equal(sequence.firstAttempt?.made, false, "opening look missed — this is the miss branch");
  assert.equal(sequence.firstAttemptsByTeam["17"]?.athleteName, "Nic Claxton");
});

test("NBA adapter separates first points from the first field goal", () => {
  const sequence = extractOpeningSequence(game);
  // A free throw is points but not a field goal — conflating the two is
  // exactly what the canonical schema exists to prevent.
  assert.equal(sequence.firstPoints?.athleteName, "Cam Thomas");
  assert.equal(sequence.firstPoints?.pointsAttempted, 1);
  assert.equal(sequence.firstFieldGoal?.athleteName, "Michael Porter Jr.");
  assert.equal(sequence.firstFieldGoal?.assistedByName, "Nikola Jokic");
  assert.notEqual(sequence.firstPoints?.playId, sequence.firstFieldGoal?.playId);
});

test("NBA adapter records each team's own first basket", () => {
  const sequence = extractOpeningSequence(game);
  assert.equal(sequence.firstFieldGoalsByTeam["7"]?.athleteName, "Michael Porter Jr.");
  assert.equal(sequence.firstFieldGoalsByTeam["17"]?.athleteName, "Cam Thomas");
});

test("NBA aggregates keep whole-game and team-first counts distinct", () => {
  const { players, teams } = aggregateSequences([extractOpeningSequence(game)]);
  const porter = players.find(player => player.player === "Michael Porter Jr.")!;
  const thomas = players.find(player => player.player === "Cam Thomas")!;
  const murray = players.find(player => player.player === "Jamal Murray")!;

  assert.equal(porter.firstFieldGoals, 1, "Porter scored the game's first field goal");
  assert.equal(porter.firstTeamFieldGoals, 1, "and Denver's own first");
  assert.equal(thomas.firstFieldGoals, 0, "Thomas did not score the game's first field goal");
  assert.equal(thomas.firstTeamFieldGoals, 1, "but he did score Brooklyn's own first");
  assert.equal(thomas.firstPoints, 1, "and the game's first points, at the line");
  assert.equal(murray.firstTeamAttempts, 1);
  assert.equal(murray.firstTeamAttemptMakes, 0);

  assert.equal(teams.find(team => team.team === "DEN")?.tipWins, 1);
  assert.equal(teams.find(team => team.team === "DEN")?.scoredFirstFieldGoal, 1);
  assert.equal(teams.find(team => team.team === "BKN")?.scoredFirstPoints, 1);
});

test("NBA candidates score from observed inputs", () => {
  const { players, teams } = aggregateSequences([extractOpeningSequence(game)]);
  const porter = players.find(player => player.player === "Michael Porter Jr.")!;
  const den = teams.find(team => team.team === "DEN")!;
  const bkn = teams.find(team => team.team === "BKN")!;
  const scored = scoreCandidate(porter, den, { opponent: bkn, roleAvailability: 1 });
  assert.equal(scored.sample.firstFieldGoals, 1);
  assert.equal(scored.rates.teamTipWin, 1);
  assert.ok(scored.edgeScore > 50);
});

test("normalizeSummary maps a raw ESPN NBA payload into the canonical shape", () => {
  const raw = {
    header: { competitions: [{ date: "2026-01-29T02:00Z", competitors: [
      { team: { id: "7", abbreviation: "DEN", displayName: "Denver Nuggets" } },
      { team: { id: "17", abbreviation: "BKN", displayName: "Brooklyn Nets" } },
    ] }] },
    boxscore: { players: [{ statistics: [{ athletes: [
      { athlete: { id: "4066211", displayName: "Michael Porter Jr." } },
    ] }] }] },
    plays: [
      { id: "17", period: { number: 1 }, text: "first quarter play" },
      { id: "180", period: { number: 2 }, text: "second quarter play" },
    ],
  };
  const summary = normalizeSummary("401705000", raw);
  assert.deepEqual(summary.teams.map(team => team.abbreviation), ["DEN", "BKN"]);
  assert.equal(summary.athletes["4066211"].name, "Michael Porter Jr.");
  assert.equal(summary.plays.length, 1, "only first-quarter plays are kept");
});

test("league config gates the NBA feed to real NBA teams", () => {
  const nba = resolveLeague("nba");
  assert.equal(nba.label, "NBA");
  for (const abbreviation of game.teams.map(team => team.abbreviation)) {
    assert.ok(nba.teams.has(abbreviation), `${abbreviation} should be a recognised NBA team`);
  }
  assert.equal(nba.teams.size, 30);
  assert.ok(!nba.teams.has("SEA"), "a WNBA-only abbreviation must not pass the NBA gate");
  assert.equal(displayAbbr(nba, "GS"), "GSW");
  assert.equal(displayAbbr(nba, "UTAH"), "UTA");
  assert.equal(displayAbbr(nba, "DEN"), "DEN");
});

test("league defaults are unchanged for the WNBA", () => {
  assert.equal(resolveLeague(undefined).key, "wnba");
  assert.equal(resolveLeague("wnba").sectionGlobal, "OPENING_EDGE");
  assert.equal(LEAGUES.nba.sectionGlobal, "OPENING_EDGE_NBA");
  assert.throws(() => resolveLeague("nhl"), /Unknown league/);
});

test("an NBA season window reaches back across New Year", () => {
  // A January slate belongs to the season that opened the previous
  // October; defaulting to October of the current year would sync an empty
  // window and publish a board with no sample behind it.
  assert.equal(defaultSeasonStart(LEAGUES.nba, new Date("2027-01-29T18:00:00Z")), "2026-10-01");
  assert.equal(defaultSeasonStart(LEAGUES.nba, new Date("2026-11-04T18:00:00Z")), "2026-10-01");
  assert.equal(defaultSeasonStart(LEAGUES.wnba, new Date("2026-08-21T18:00:00Z")), "2026-05-01");
});
