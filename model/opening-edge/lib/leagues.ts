// League configuration for the Opening Edge first-basket model.
//
// The vendored basketball core (lib/wnba/*) is league-agnostic maths: an
// opening sequence, per-player and per-team aggregates, and the edge score
// all read the same ESPN play-by-play shape whether the game is a WNBA game
// or an NBA one. The only league-specific facts are which ESPN endpoint to
// pull, which team abbreviations count as that league's, how those
// abbreviations are displayed on the board, where headshots live, and when
// the season starts. Those live here so adding a league is a config entry
// rather than a fork of the model.
//
// WNBA remains the default everywhere, so every existing call site behaves
// exactly as it did before this file existed.

export type LeagueKey = "wnba" | "nba";

export type LeagueConfig = {
  key: LeagueKey;
  /** Display name used in banners, output payloads and log lines. */
  label: string;
  /** ESPN site-API base for this league's scoreboard / summary / roster. */
  espnBase: string;
  /** Abbreviations that identify a real league game (filters out exhibitions
   *  and cross-league events that share the same ESPN endpoints). */
  teams: Set<string>;
  /** ESPN abbreviation -> the abbreviation the board shows. */
  display: Record<string, string>;
  /** Headshot URL prefix; the athlete id and ".png" are appended. */
  headshotBase: string;
  /** Model snapshot / board snapshot written by the sync script. */
  modelFile: string;
  boardFile: string;
  /** Current-roster map (sync-rosters.ts) and triple-double watch
   *  (sync-td-watch.ts). Both are optional side inputs: the generator
   *  degrades to no roster validation / no watch list if they are absent. */
  rosterFile: string;
  watchFile: string;
  /** Generated section the page loads, relative to the repo root. */
  sectionFile: string;
  /** Global the generated section assigns itself to. */
  sectionGlobal: string;
  /** Month (1-12) the regular season opens in. Used to resolve a default
   *  sync window for a season that crosses a calendar year. */
  seasonStartMonth: number;
  /** Minimum whole-game first baskets a player needs to earn a pick row.
   *  The NBA plays roughly 2.4x the WNBA's games, so the same evidence bar
   *  is a higher raw count. */
  minFirstBaskets: number;
};

export const LEAGUES: Record<LeagueKey, LeagueConfig> = {
  wnba: {
    key: "wnba",
    label: "WNBA",
    espnBase: "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba",
    teams: new Set(["ATL", "CHI", "CON", "DAL", "GS", "IND", "LA", "LV", "MIN", "NY", "PHX", "POR", "SEA", "TOR", "WSH"]),
    display: { WSH: "WAS", NY: "NYL", GS: "GSV", LV: "LVA" },
    headshotBase: "https://a.espncdn.com/i/headshots/wnba/players/full/",
    modelFile: "data/wnba-model.json",
    boardFile: "data/wnba-board.json",
    rosterFile: "data/rosters.json",
    watchFile: "data/td-watch.json",
    sectionFile: "../../data/opening-edge.js",
    sectionGlobal: "OPENING_EDGE",
    seasonStartMonth: 5,
    minFirstBaskets: 2,
  },
  nba: {
    key: "nba",
    label: "NBA",
    espnBase: "https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
    teams: new Set([
      "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GS",
      "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NO", "NY",
      "OKC", "ORL", "PHI", "PHX", "POR", "SA", "SAC", "TOR", "UTAH", "WSH",
    ]),
    display: { GS: "GSW", NO: "NOP", NY: "NYK", SA: "SAS", UTAH: "UTA", WSH: "WAS" },
    headshotBase: "https://a.espncdn.com/i/headshots/nba/players/full/",
    modelFile: "data/nba-model.json",
    boardFile: "data/nba-board.json",
    rosterFile: "data/nba-rosters.json",
    watchFile: "data/nba-td-watch.json",
    sectionFile: "../../data/opening-edge-nba.js",
    sectionGlobal: "OPENING_EDGE_NBA",
    seasonStartMonth: 10,
    minFirstBaskets: 3,
  },
};

export function resolveLeague(key: string | undefined | null): LeagueConfig {
  const resolved = LEAGUES[(key ?? "wnba").toLowerCase() as LeagueKey];
  if (!resolved) throw new Error(`Unknown league "${key}" — expected one of ${Object.keys(LEAGUES).join(", ")}`);
  return resolved;
}

/** First day of the season in progress on `today`.
 *
 * The WNBA season opens and closes inside one calendar year, so this is
 * always May of the current year. The NBA season crosses New Year, so a
 * January slate belongs to the season that opened the previous October —
 * defaulting to October of the current year there would sync an empty
 * window and silently publish a board with no sample behind it.
 */
export function defaultSeasonStart(league: LeagueConfig, today: Date = new Date()): string {
  const year = Number(new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric" }).format(today));
  const month = Number(new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", month: "numeric" }).format(today));
  const seasonYear = month >= league.seasonStartMonth ? year : year - 1;
  return `${seasonYear}-${String(league.seasonStartMonth).padStart(2, "0")}-01`;
}

export function displayAbbr(league: LeagueConfig, abbr: string): string {
  return league.display[abbr] ?? abbr;
}
