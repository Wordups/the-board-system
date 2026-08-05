import type { OpeningSequence, PlayerAggregate, TeamAggregate } from "./types.ts";

// ESPN occasionally carries duplicate athlete records for one player —
// e.g. Natasha Cloud appears as both 2529137 and 3142010 in 2026 CHI
// play-by-play. Alias stale ids to the canonical record before
// aggregating so one player's counts merge into a single row.
const ATHLETE_ALIASES: Record<string, string> = {
  "2529137": "3142010", // Natasha Cloud
};

function aliasEvent<T extends { athleteId?: string } | null | undefined>(event: T): T {
  if (event?.athleteId && ATHLETE_ALIASES[event.athleteId]) event.athleteId = ATHLETE_ALIASES[event.athleteId];
  return event;
}

export function aggregateSequences(sequences: OpeningSequence[]) {
  const players = new Map<string, PlayerAggregate>();
  const teams = new Map<string, TeamAggregate>();

  for (const sequence of sequences) {
    aliasEvent(sequence.firstAttempt);
    aliasEvent(sequence.firstFieldGoal);
    aliasEvent(sequence.firstPoints);
    for (const key of Object.keys(sequence.firstAttemptsByTeam)) aliasEvent(sequence.firstAttemptsByTeam[key]);
    for (const key of Object.keys(sequence.firstFieldGoalsByTeam)) aliasEvent(sequence.firstFieldGoalsByTeam[key]);
    for (const team of sequence.teams) {
      const current = teams.get(team.id) ?? { teamId: team.id, team: team.abbreviation, games: 0, tipWins: 0, scoredFirstFieldGoal: 0, scoredFirstPoints: 0, convertedFirstAttempt: 0 };
      current.games += 1;
      if (sequence.tip.winningTeamId === team.id) current.tipWins += 1;
      if (sequence.firstFieldGoal?.teamId === team.id) current.scoredFirstFieldGoal += 1;
      if (sequence.firstPoints?.teamId === team.id) current.scoredFirstPoints += 1;
      if (sequence.firstAttempt?.teamId === team.id && sequence.firstAttempt.made) current.convertedFirstAttempt += 1;
      teams.set(team.id, current);
    }

    const appearances = new Set<string>();
    for (const event of Object.values(sequence.firstAttemptsByTeam)) if (event?.athleteId) appearances.add(event.athleteId);
    for (const event of Object.values(sequence.firstFieldGoalsByTeam)) if (event?.athleteId) appearances.add(event.athleteId);
    if (sequence.firstFieldGoal?.athleteId) appearances.add(sequence.firstFieldGoal.athleteId);
    if (sequence.firstPoints?.athleteId) appearances.add(sequence.firstPoints.athleteId);

    for (const athleteId of appearances) {
      const event = Object.values(sequence.firstAttemptsByTeam).find(item => item?.athleteId === athleteId)
        ?? Object.values(sequence.firstFieldGoalsByTeam).find(item => item?.athleteId === athleteId)
        ?? sequence.firstFieldGoal;
      if (!event?.teamId) continue;
      const playerKey = `${event.teamId}:${athleteId}`;
      const current = players.get(playerKey) ?? { athleteId, player: event.athleteName ?? athleteId, teamId: event.teamId, games: 0, firstAttempts: 0, firstAttemptMakes: 0, firstFieldGoals: 0, firstPoints: 0, firstTeamAttempts: 0, firstTeamAttemptMakes: 0, firstTeamFieldGoals: 0, assistedOpeningMakes: 0 };
      current.games += 1;
      if (sequence.firstAttempt?.athleteId === athleteId) {
        current.firstAttempts += 1;
        if (sequence.firstAttempt.made) current.firstAttemptMakes += 1;
      }
      if (sequence.firstFieldGoal?.athleteId === athleteId) current.firstFieldGoals += 1;
      if (sequence.firstPoints?.athleteId === athleteId) current.firstPoints += 1;
      if (sequence.firstAttemptsByTeam[event.teamId]?.athleteId === athleteId) {
        current.firstTeamAttempts += 1;
        if (sequence.firstAttemptsByTeam[event.teamId]?.made) current.firstTeamAttemptMakes += 1;
      }
      if (sequence.firstFieldGoalsByTeam[event.teamId]?.athleteId === athleteId) {
        current.firstTeamFieldGoals += 1;
        if (sequence.firstFieldGoalsByTeam[event.teamId]?.assistedById) current.assistedOpeningMakes += 1;
      }
      players.set(playerKey, current);
    }
  }
  return { players: [...players.values()], teams: [...teams.values()] };
}
