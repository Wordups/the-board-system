// Shared season-sync routine behind scripts/sync-wnba.ts and
// scripts/sync-nba.ts.
//
// Both entrypoints do the same three things -- build the model over a date
// window, write the full snapshot, write the compact board -- and differ
// only in the league config they pass and the files they land in (see
// lib/leagues.ts). Keeping the body here means a fix to the sync applies to
// every league instead of to whichever copy someone remembered to edit.
import { mkdir, writeFile } from "node:fs/promises";
import { defaultSeasonStart, type LeagueConfig } from "./leagues.ts";
import { buildModel } from "./wnba/model.ts";

export async function syncLeague(league: LeagueConfig, start?: string, end?: string) {
  const windowEnd = end ?? new Date().toISOString().slice(0, 10);
  const windowStart = start ?? defaultSeasonStart(league);
  const model = await buildModel(windowStart, windowEnd, league);
  await mkdir("data", { recursive: true });
  await writeFile(league.modelFile, JSON.stringify(model, null, 2), "utf8");
  await writeFile(league.boardFile, JSON.stringify({
    generatedAt: model.generatedAt,
    league: model.league,
    leagueLabel: model.leagueLabel,
    start: model.start,
    end: model.end,
    games: model.games,
    source: model.source,
    marketDefinitions: model.marketDefinitions,
    teams: model.teams,
    players: model.players,
    candidates: model.candidates,
  }, null, 2), "utf8");
  console.log(
    `${league.label}: wrote ${model.games} games, ${model.players.length} players, ` +
    `${model.candidates.length} candidates (${windowStart} → ${windowEnd}) to ${league.modelFile}`,
  );
  return model;
}
