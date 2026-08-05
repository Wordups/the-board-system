import type { ModelCandidate, PlayerAggregate, TeamAggregate } from "./types.ts";

export type CandidateContext = {
  opponent?: TeamAggregate;
  roleAvailability?: number;
  headToHead?: number;
  marketValue?: number;
};

export function scoreCandidate(player: PlayerAggregate, team: TeamAggregate, context: CandidateContext = {}): ModelCandidate {
  const rate = (n: number, d: number) => d ? n / d : 0;
  const firstBasket = rate(player.firstFieldGoals, team.games);
  const firstAttempt = rate(player.firstTeamAttempts, team.games);
  const firstAttemptMake = rate(player.firstTeamAttemptMakes, player.firstTeamAttempts);
  const teamTipWin = rate(team.tipWins, team.games);
  const opponentTipWin = context.opponent ? rate(context.opponent.tipWins, context.opponent.games) : .5;
  const tipMatchup = clamp((teamTipWin + (1 - opponentTipWin)) / 2);
  const teamScoresFirst = rate(team.scoredFirstFieldGoal, team.games);
  const components = {
    playerFirstBasketShare: clamp(firstBasket),
    teamFirstBasketRate: clamp(teamScoresFirst),
    firstShotInvolvement: clamp(firstAttempt * .7 + firstAttemptMake * .3),
    tipMatchup,
    roleAvailability: context.roleAvailability ?? .5,
    headToHead: context.headToHead ?? .5,
    marketValue: context.marketValue ?? .5,
  };
  const edgeScore = Math.round(100 * (
    components.playerFirstBasketShare * .30 +
    components.teamFirstBasketRate * .20 +
    components.firstShotInvolvement * .15 +
    components.tipMatchup * .15 +
    components.roleAvailability * .10 +
    components.headToHead * .05 +
    components.marketValue * .05
  ));
  return {
    athleteId: player.athleteId, player: player.player, teamId: team.teamId, team: team.team,
    opponentId: context.opponent?.teamId, games: team.games, edgeScore, components,
    rates: { firstBasket, firstAttempt, firstAttemptMake, teamTipWin, teamScoresFirst },
    sample: { firstFieldGoals: player.firstFieldGoals, firstAttempts: player.firstTeamAttempts, firstAttemptMakes: player.firstTeamAttemptMakes, teamGames: team.games },
  };
}

export function rankCandidates(players: PlayerAggregate[], teams: TeamAggregate[], matchups: Record<string, string> = {}) {
  const teamMap = new Map(teams.map(team => [team.teamId, team]));
  return players.map(player => {
    const team = teamMap.get(player.teamId);
    if (!team) return null;
    return scoreCandidate(player, team, { opponent: teamMap.get(matchups[player.teamId]) });
  }).filter((candidate): candidate is ModelCandidate => candidate !== null).sort((a, b) => b.edgeScore - a.edgeScore || b.sample.firstFieldGoals - a.sample.firstFieldGoals);
}

function clamp(value: number) { return Math.max(0, Math.min(1, value)); }
