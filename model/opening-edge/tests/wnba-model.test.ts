import assert from "node:assert/strict";
import test from "node:test";
import { extractOpeningSequence } from "../lib/wnba/opening-sequence.ts";
import { aggregateSequences } from "../lib/wnba/aggregate.ts";
import { scoreCandidate } from "../lib/wnba/score.ts";
import type { GameSummary } from "../lib/wnba/types.ts";

const game: GameSummary = {
  gameId: "401857107",
  date: "2026-08-02T17:00Z",
  name: "Indiana Fever at Minnesota Lynx",
  teams: [
    { id: "5", abbreviation: "IND", displayName: "Indiana Fever" },
    { id: "8", abbreviation: "MIN", displayName: "Minnesota Lynx" },
  ],
  athletes: {
    "4432831": { id: "4432831", name: "Aliyah Boston" },
    "2529130": { id: "2529130", name: "Natasha Howard" },
    "3142255": { id: "3142255", name: "Monique Billings" },
    "3917450": { id: "3917450", name: "Napheesa Collier" },
  },
  plays: [
    { id: "4", sequenceNumber: "4", type: { text: "Jumpball" }, text: "Aliyah Boston vs. Natasha Howard (Monique Billings gains possession)", period: { number: 1 }, clock: { displayValue: "10:00" }, team: { id: "5" }, participants: [{ athlete: { id: "4432831" } }, { athlete: { id: "2529130" } }, { athlete: { id: "3142255" } }] },
    { id: "7", sequenceNumber: "7", type: { text: "Driving Layup Shot" }, text: "Aliyah Boston makes driving layup", period: { number: 1 }, clock: { displayValue: "9:38" }, team: { id: "5" }, participants: [{ athlete: { id: "4432831" } }], shootingPlay: true, scoringPlay: true, scoreValue: 2, pointsAttempted: 2 },
    { id: "9", sequenceNumber: "9", type: { text: "Alley Oop Layup Shot" }, text: "Napheesa Collier misses alley oop layup", period: { number: 1 }, clock: { displayValue: "9:23" }, team: { id: "8" }, participants: [{ athlete: { id: "3917450" } }], shootingPlay: true, scoringPlay: false, scoreValue: 0, pointsAttempted: 2 },
    { id: "14", sequenceNumber: "14", type: { text: "Layup Shot" }, text: "Natasha Howard makes layup", period: { number: 1 }, clock: { displayValue: "8:58" }, team: { id: "8" }, participants: [{ athlete: { id: "2529130" } }], shootingPlay: true, scoringPlay: true, scoreValue: 2, pointsAttempted: 2 },
  ],
};

test("extracts tip, first attempt, and whole-game first basket", () => {
  const sequence = extractOpeningSequence(game);
  assert.equal(sequence.tip.winningTeamId, "5");
  assert.equal(sequence.tip.possessionPlayerName, "Monique Billings");
  assert.equal(sequence.firstAttempt?.athleteName, "Aliyah Boston");
  assert.equal(sequence.firstFieldGoal?.athleteName, "Aliyah Boston");
  assert.equal(sequence.firstAttemptsByTeam["8"]?.athleteName, "Napheesa Collier");
  assert.equal(sequence.firstFieldGoalsByTeam["8"]?.athleteName, "Natasha Howard");
});

test("aggregates whole-game and first-team events separately", () => {
  const sequence = extractOpeningSequence(game);
  const { players, teams } = aggregateSequences([sequence]);
  const boston = players.find(player => player.player === "Aliyah Boston");
  const collier = players.find(player => player.player === "Napheesa Collier");
  assert.equal(boston?.firstFieldGoals, 1);
  assert.equal(boston?.firstTeamAttempts, 1);
  assert.equal(boston?.firstTeamAttemptMakes, 1);
  assert.equal(collier?.firstFieldGoals, 0);
  assert.equal(collier?.firstTeamAttempts, 1);
  assert.equal(collier?.firstTeamAttemptMakes, 0);
  assert.equal(teams.find(team => team.team === "IND")?.tipWins, 1);
});

test("scores from observed inputs rather than a hardcoded rank", () => {
  const sequence = extractOpeningSequence(game);
  const { players, teams } = aggregateSequences([sequence]);
  const boston = players.find(player => player.player === "Aliyah Boston")!;
  const ind = teams.find(team => team.team === "IND")!;
  const min = teams.find(team => team.team === "MIN")!;
  const scored = scoreCandidate(boston, ind, { opponent: min, roleAvailability: 1, headToHead: .5, marketValue: .5 });
  assert.equal(scored.sample.firstFieldGoals, 1);
  assert.equal(scored.rates.teamTipWin, 1);
  assert.ok(scored.edgeScore > 70);
});

test("keeps the same athlete's records separate after a trade", () => {
  const second = structuredClone(game);
  second.gameId = "trade-game";
  second.teams[0] = { id: "99", abbreviation: "NEW", displayName: "New Team" };
  second.plays = second.plays.map(play => play.team?.id === "5" ? { ...play, team: { id: "99" } } : play);
  const { players } = aggregateSequences([extractOpeningSequence(game), extractOpeningSequence(second)]);
  const bostonRows = players.filter(player => player.player === "Aliyah Boston");
  assert.equal(bostonRows.length, 2);
  assert.deepEqual(new Set(bostonRows.map(player => player.teamId)), new Set(["5", "99"]));
});
