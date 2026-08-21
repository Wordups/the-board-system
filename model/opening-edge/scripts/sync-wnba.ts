// Sync the WNBA season snapshot from ESPN play-by-play.
//
//   node --experimental-strip-types scripts/sync-wnba.ts [START] [END]
//
// Writes data/wnba-model.json (full snapshot with sequences) and
// data/wnba-board.json (the compact board the generator reads). The body
// lives in lib/sync.ts, shared with scripts/sync-nba.ts.
import { LEAGUES } from "../lib/leagues.ts";
import { syncLeague } from "../lib/sync.ts";

await syncLeague(LEAGUES.wnba, process.argv[2], process.argv[3]);
