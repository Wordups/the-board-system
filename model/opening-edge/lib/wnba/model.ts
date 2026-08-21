import { LEAGUES, type LeagueConfig } from "../leagues.ts";
import { aggregateSequences } from "./aggregate.ts";
import { fetchDateRange } from "./espn.ts";
import { extractOpeningSequence } from "./opening-sequence.ts";
import { rankCandidates } from "./score.ts";

export async function buildModel(start: string, end: string, league: LeagueConfig = LEAGUES.wnba) {
  const games = await fetchDateRange(start, end, league);
  const sequences = games.filter(game => game.plays.length).map(extractOpeningSequence);
  const aggregates = aggregateSequences(sequences);
  return {
    generatedAt: new Date().toISOString(), start, end,
    league: league.key,
    leagueLabel: league.label,
    source: `ESPN play-by-play; verify against official ${league.label} gamebook for wagering decisions`,
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
