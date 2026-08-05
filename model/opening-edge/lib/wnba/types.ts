export type MarketDefinition = "first_field_goal" | "first_points";

export type EspnPlay = {
  id: string;
  sequenceNumber?: string;
  type?: { id?: string; text?: string };
  text?: string;
  awayScore?: number;
  homeScore?: number;
  period?: { number?: number; displayValue?: string };
  clock?: { displayValue?: string };
  scoringPlay?: boolean;
  scoreValue?: number;
  shootingPlay?: boolean;
  pointsAttempted?: number;
  team?: { id?: string };
  participants?: Array<{ athlete?: { id?: string } }>;
};

export type Athlete = { id: string; name: string };
export type Team = { id: string; abbreviation: string; displayName: string };

export type OpeningEvent = {
  playId: string;
  clock: string;
  teamId: string | null;
  athleteId: string | null;
  athleteName: string | null;
  text: string;
  made: boolean;
  pointsAttempted: number;
  assistedById: string | null;
  assistedByName: string | null;
};

export type OpeningSequence = {
  gameId: string;
  date: string;
  teams: Team[];
  tip: {
    playId: string | null;
    winningTeamId: string | null;
    possessionPlayerId: string | null;
    possessionPlayerName: string | null;
    text: string | null;
  };
  firstAttempt: OpeningEvent | null;
  firstFieldGoal: OpeningEvent | null;
  firstPoints: OpeningEvent | null;
  firstAttemptsByTeam: Record<string, OpeningEvent | null>;
  firstFieldGoalsByTeam: Record<string, OpeningEvent | null>;
  openingPlays: Array<{
    playId: string;
    clock: string;
    teamId: string | null;
    type: string;
    text: string;
  }>;
};

export type GameSummary = {
  gameId: string;
  date: string;
  name: string;
  teams: Team[];
  athletes: Record<string, Athlete>;
  plays: EspnPlay[];
};

export type PlayerAggregate = {
  athleteId: string;
  player: string;
  teamId: string;
  games: number;
  firstAttempts: number;
  firstAttemptMakes: number;
  firstFieldGoals: number;
  firstPoints: number;
  firstTeamAttempts: number;
  firstTeamAttemptMakes: number;
  firstTeamFieldGoals: number;
  assistedOpeningMakes: number;
};

export type TeamAggregate = {
  teamId: string;
  team: string;
  games: number;
  tipWins: number;
  scoredFirstFieldGoal: number;
  scoredFirstPoints: number;
  convertedFirstAttempt: number;
};

export type ModelCandidate = {
  athleteId: string;
  player: string;
  teamId: string;
  team: string;
  opponentId?: string;
  games: number;
  edgeScore: number;
  components: {
    playerFirstBasketShare: number;
    teamFirstBasketRate: number;
    firstShotInvolvement: number;
    tipMatchup: number;
    roleAvailability: number;
    headToHead: number;
    marketValue: number;
  };
  rates: {
    firstBasket: number;
    firstAttempt: number;
    firstAttemptMake: number;
    teamTipWin: number;
    teamScoresFirst: number;
  };
  sample: {
    firstFieldGoals: number;
    firstAttempts: number;
    firstAttemptMakes: number;
    teamGames: number;
  };
};
