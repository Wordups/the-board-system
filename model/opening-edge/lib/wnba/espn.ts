import { LEAGUES, type LeagueConfig } from "../leagues.ts";
import type { Athlete, EspnPlay, GameSummary, Team } from "./types.ts";

// The basketball opening model reads the same ESPN play-by-play shape for
// every league; only the endpoint and the set of real league teams differ.
// Both are supplied by lib/leagues.ts, defaulting to WNBA so every call
// site that predates multi-league support behaves exactly as before.
const DEFAULT_LEAGUE = LEAGUES.wnba;

export async function fetchScoreboard(date: string, league: LeagueConfig = DEFAULT_LEAGUE): Promise<Array<{ id: string; name: string; date: string }>> {
  const compact = date.replaceAll("-", "");
  const response = await fetch(`${league.espnBase}/scoreboard?dates=${compact}`, { next: { revalidate: 300 } });
  if (!response.ok) throw new Error(`ESPN scoreboard ${response.status}`);
  const json = await response.json() as { events?: Array<{ id: string; name: string; date: string }> };
  return json.events ?? [];
}

export async function fetchGameSummary(gameId: string, league: LeagueConfig = DEFAULT_LEAGUE): Promise<GameSummary> {
  const response = await fetch(`${league.espnBase}/summary?event=${gameId}`, { next: { revalidate: 86400 } });
  if (!response.ok) throw new Error(`ESPN summary ${gameId}: ${response.status}`);
  const json = await response.json() as Record<string, unknown>;
  return normalizeSummary(gameId, json);
}

export function normalizeSummary(gameId: string, json: Record<string, unknown>): GameSummary {
  const header = json.header as { competitions?: Array<{ date?: string; competitors?: Array<{ team?: Record<string, unknown> }> }> } | undefined;
  const competition = header?.competitions?.[0];
  const teams: Team[] = (competition?.competitors ?? []).map(({ team }) => ({
    id: String(team?.id ?? ""),
    abbreviation: String(team?.abbreviation ?? team?.shortDisplayName ?? ""),
    displayName: String(team?.displayName ?? team?.name ?? ""),
  })).filter(team => team.id);

  const athletes: Record<string, Athlete> = {};
  const boxscore = json.boxscore as { players?: Array<{ statistics?: Array<{ athletes?: Array<{ athlete?: Record<string, unknown> }> }> }> } | undefined;
  for (const team of boxscore?.players ?? []) {
    for (const stat of team.statistics ?? []) {
      for (const row of stat.athletes ?? []) {
        const athlete = row.athlete;
        const id = String(athlete?.id ?? "");
        if (id) athletes[id] = { id, name: String(athlete?.displayName ?? athlete?.shortName ?? id) };
      }
    }
  }

  const plays = ((json.plays as EspnPlay[] | undefined) ?? []).filter(play => play.period?.number === 1);
  return {
    gameId,
    date: String(competition?.date ?? ""),
    name: teams.map(team => team.displayName).join(" vs "),
    teams,
    athletes,
    plays,
  };
}

export async function fetchDateRange(start: string, end: string, league: LeagueConfig = DEFAULT_LEAGUE): Promise<GameSummary[]> {
  const dates = enumerateDates(start, end);
  const events = (await Promise.all(dates.map(date => fetchScoreboard(date, league)))).flat();
  const unique = [...new Map(events.map(event => [event.id, event])).values()];
  const summaries: GameSummary[] = [];
  for (let index = 0; index < unique.length; index += 6) {
    const batch = unique.slice(index, index + 6);
    const fetched = await Promise.all(batch.map(event => fetchGameSummary(event.id, league)));
    summaries.push(...fetched.filter(game => game.teams.length === 2 && game.teams.every(team => league.teams.has(team.abbreviation))));
  }
  return summaries;
}

function enumerateDates(start: string, end: string): string[] {
  const dates: string[] = [];
  const cursor = new Date(`${start}T12:00:00Z`);
  const last = new Date(`${end}T12:00:00Z`);
  while (cursor <= last) {
    dates.push(cursor.toISOString().slice(0, 10));
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return dates;
}
