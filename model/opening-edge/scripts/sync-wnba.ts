import { mkdir, writeFile } from "node:fs/promises";
import { buildModel } from "../lib/wnba/model.ts";

const end = process.argv[3] ?? new Date().toISOString().slice(0, 10);
const start = process.argv[2] ?? `${new Date().getUTCFullYear()}-05-01`;
const model = await buildModel(start, end);
await mkdir("data", { recursive: true });
await writeFile("data/wnba-model.json", JSON.stringify(model, null, 2), "utf8");
await writeFile("data/wnba-board.json", JSON.stringify({
  generatedAt: model.generatedAt,
  start: model.start,
  end: model.end,
  games: model.games,
  source: model.source,
  marketDefinitions: model.marketDefinitions,
  teams: model.teams,
  players: model.players,
  candidates: model.candidates,
}, null, 2), "utf8");
console.log(`Wrote ${model.games} games, ${model.players.length} players, ${model.candidates.length} candidates to data/wnba-model.json`);
