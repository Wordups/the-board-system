// Sync the NBA season snapshot from ESPN play-by-play.
//
//   node --experimental-strip-types scripts/sync-nba.ts [START] [END]
//
// Writes data/nba-model.json and data/nba-board.json. Same adapter as the
// WNBA sync (ESPN basketball play-by-play, validated by
// tests/nba-model.test.ts against a known-game fixture per AGENT.md's
// new-league rule) with the NBA endpoint, team set and season window from
// lib/leagues.ts.
//
// START defaults to October 1 of the season in progress, not of the current
// calendar year -- an NBA season crosses New Year, so a January refresh has
// to reach back into the previous one.
import { LEAGUES } from "../lib/leagues.ts";
import { syncLeague } from "../lib/sync.ts";

await syncLeague(LEAGUES.nba, process.argv[2], process.argv[3]);
