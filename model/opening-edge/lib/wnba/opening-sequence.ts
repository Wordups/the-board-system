import type { EspnPlay, GameSummary, OpeningEvent, OpeningSequence } from "./types.ts";

export function extractOpeningSequence(game: GameSummary): OpeningSequence {
  const plays = [...game.plays].sort((a, b) => Number(a.sequenceNumber ?? 0) - Number(b.sequenceNumber ?? 0));
  const jump = plays.find(play => /jumpball|jump ball/i.test(`${play.type?.text ?? ""} ${play.text ?? ""}`));
  const firstAttemptPlay = plays.find(play => Boolean(play.shootingPlay));
  const firstFieldGoalPlay = plays.find(play => Boolean(play.shootingPlay && play.scoringPlay && (play.scoreValue ?? 0) >= 2));
  const firstPointsPlay = plays.find(play => Boolean(play.scoringPlay && (play.scoreValue ?? 0) > 0));
  const firstAttemptsByTeam: Record<string, OpeningEvent | null> = {};
  const firstFieldGoalsByTeam: Record<string, OpeningEvent | null> = {};

  for (const team of game.teams) {
    firstAttemptsByTeam[team.id] = toOpeningEvent(plays.find(play => play.team?.id === team.id && play.shootingPlay), game);
    firstFieldGoalsByTeam[team.id] = toOpeningEvent(plays.find(play => play.team?.id === team.id && play.shootingPlay && play.scoringPlay && (play.scoreValue ?? 0) >= 2), game);
  }

  const gainName = jump?.text?.match(/\((.+?) gains possession\)/i)?.[1] ?? null;
  const gainId = gainName ? Object.values(game.athletes).find(athlete => athlete.name === gainName)?.id ?? participantId(jump, 2) : participantId(jump, 2);

  return {
    gameId: game.gameId,
    date: game.date,
    teams: game.teams,
    tip: {
      playId: jump?.id ?? null,
      winningTeamId: jump?.team?.id ?? null,
      possessionPlayerId: gainId,
      possessionPlayerName: gainId ? game.athletes[gainId]?.name ?? gainName : gainName,
      text: jump?.text ?? null,
    },
    firstAttempt: toOpeningEvent(firstAttemptPlay, game),
    firstFieldGoal: toOpeningEvent(firstFieldGoalPlay, game),
    firstPoints: toOpeningEvent(firstPointsPlay, game),
    firstAttemptsByTeam,
    firstFieldGoalsByTeam,
    openingPlays: plays.slice(0, 12).map(play => ({
      playId: play.id,
      clock: play.clock?.displayValue ?? "",
      teamId: play.team?.id ?? null,
      type: play.type?.text ?? "",
      text: play.text ?? "",
    })),
  };
}

function participantId(play: EspnPlay | undefined, index = 0): string | null {
  return play?.participants?.[index]?.athlete?.id ?? null;
}

function toOpeningEvent(play: EspnPlay | undefined, game: GameSummary): OpeningEvent | null {
  if (!play) return null;
  const athleteId = participantId(play);
  const assistName = play.text?.match(/\((.+?) assists?\)/i)?.[1] ?? null;
  const assistedById = assistName ? Object.values(game.athletes).find(athlete => athlete.name === assistName)?.id ?? participantId(play, 1) : null;
  return {
    playId: play.id,
    clock: play.clock?.displayValue ?? "",
    teamId: play.team?.id ?? null,
    athleteId,
    athleteName: athleteId ? game.athletes[athleteId]?.name ?? null : null,
    text: play.text ?? "",
    made: Boolean(play.scoringPlay),
    pointsAttempted: play.pointsAttempted ?? play.scoreValue ?? 0,
    assistedById,
    assistedByName: assistedById ? game.athletes[assistedById]?.name ?? assistName : assistName,
  };
}
