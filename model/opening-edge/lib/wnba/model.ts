import { aggregateSequences } from "./aggregate.ts";
import { fetchDateRange } from "./espn.ts";
import { extractOpeningSequence } from "./opening-sequence.ts";
import { rankCandidates } from "./score.ts";

export async function buildModel(start: string, end: string) {
  const games = await fetchDateRange(start, end);
  const sequences = games.filter(game => game.plays.length).map(extractOpeningSequence);
  const aggregates = aggregateSequences(sequences);
  return {
    generatedAt: new Date().toISOString(), start, end,
    source: "ESPN play-by-play; verify against official WNBA gamebook for wagering decisions",
    marketDefinitions: {
      firstFieldGoal: "First made field goal by either team; free throws excluded",
      firstPoints: "First points by either team; free throws included",
    },
    games: sequences.length,
    sequences,
    ...aggregates,
    candidates: rankCandidates(aggregates.players, aggregates.teams),
  };
}
