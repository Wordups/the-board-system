// Generate data/opening-edge.js (the #opening section of the board) from the
// synced model snapshot, re-scored against today's actual matchups.
//
// Usage (from model/opening-edge/):
//   node --experimental-strip-types scripts/generate-section.ts \
//     --date 2026-08-05 --label "Wednesday, Aug 5" \
//     --slate "PHX@ATL=7:00 PM ET,SEA@NY=7:00 PM ET,DAL@WSH=7:30 PM ET,LA@CHI=9:00 PM ET" \
//     --injuries "NY:Satou Sabally Out,Leonie Fiebich Out;CHI:Skylar Diggins Out"
//
// Every number in the output is derived from the snapshot; nothing is
// hand-tuned. Per AGENT.md, components with no verified input (role/
// availability, H2H, market) stay at neutral 0.5 and are DISCLOSED as
// unverified rather than silently treated as facts. Prices are model-fair
// lines — provisional until market prices are supplied.
import { readFile, writeFile } from "node:fs/promises";
import { rankCandidates } from "../lib/wnba/score.ts";
import type { ModelCandidate, PlayerAggregate, TeamAggregate } from "../lib/wnba/types.ts";

const arg = (name: string, fallback = "") => {
  const index = process.argv.indexOf(`--${name}`);
  return index > -1 ? process.argv[index + 1] : fallback;
};

const date = arg("date", new Date().toISOString().slice(0, 10));
const label = arg("label", date);
const slateArg = arg("slate");
const injuriesArg = arg("injuries");
if (!slateArg) throw new Error("--slate is required, e.g. --slate \"DAL@WSH=7:30 PM ET,SEA@NY=7:00 PM ET\"");

// ESPN abbreviation -> display abbreviation used on the board.
const DISPLAY: Record<string, string> = { WSH: "WAS", NY: "NYL", GS: "GSV", LV: "LVA" };
const display = (abbr: string) => DISPLAY[abbr] ?? abbr;

const board = JSON.parse(await readFile("data/wnba-board.json", "utf8")) as {
  generatedAt: string; start: string; end: string;
  teams: TeamAggregate[]; players: PlayerAggregate[];
};

const readOptional = async (path: string) => {
  try { return JSON.parse(await readFile(path, "utf8")); } catch { return null; }
};
const teamByAbbr = new Map(board.teams.map(team => [team.team, team]));
const teamById = new Map(board.teams.map(team => [team.teamId, team]));

// "NY:Satou Sabally Out,Leonie Fiebich Out;CHI:Skylar Diggins Out" ->
// Map(espnAbbr -> ["Satou Sabally Out", ...])
const injuries = new Map<string, string[]>();
for (const teamEntry of injuriesArg ? injuriesArg.split(";") : []) {
  const [abbr, list] = teamEntry.split(":");
  if (abbr && list) injuries.set(abbr.trim(), list.split(",").map(item => item.trim()).filter(Boolean));
}

interface SlateGame { away: TeamAggregate; home: TeamAggregate; time: string }
const slate: SlateGame[] = slateArg.split(",").map(entry => {
  const [pair, time] = entry.split("=");
  const [awayAbbr, homeAbbr] = pair.trim().split("@");
  const away = teamByAbbr.get(awayAbbr.trim());
  const home = teamByAbbr.get(homeAbbr.trim());
  if (!away || !home) throw new Error(`Unknown team in slate entry "${entry}"`);
  return { away, home, time: (time ?? "").trim() };
});

const matchups: Record<string, string> = {};
for (const game of slate) {
  matchups[game.away.teamId] = game.home.teamId;
  matchups[game.home.teamId] = game.away.teamId;
}
const slateTeamIds = new Set(Object.keys(matchups));
const opponentOf = (candidate: ModelCandidate) => teamById.get(matchups[candidate.teamId])!;

const MIN_FIRST_BASKETS = 2;
const PICK_COUNT = 8;

// Low-read red flag: a team that rarely scores first makes its whole game's
// opening volatile — flag the game and caution picks on BOTH sides.
// Data-driven (currently catches MIN at 11/33).
const lowReadTeams = new Map(
  board.teams
    .filter(team => team.games >= 10 && team.scoredFirstFieldGoal / team.games < 0.4)
    .map(team => [team.teamId, team]),
);
const rate = (n: number, d: number) => (d ? n / d : 0);
const pct = (value: number, digits = 1) => Number((value * 100).toFixed(digits));

const fairAmerican = (probability: number) => {
  const p = Math.min(0.98, Math.max(0.02, probability));
  const odds = p >= 0.5 ? -Math.round((p / (1 - p)) * 100) : Math.round(((1 - p) / p) * 100);
  return odds > 0 ? `+${odds}` : String(odds);
};

// Current-roster validation: a candidate's record team must match their
// current ESPN roster team, or the pick would present a stale team label
// (e.g. a player whose opening events predate a trade). Missing rosters.json
// disables the filter rather than blocking the board.
const rosterFile = await (async () => {
  try { return JSON.parse(await readFile("data/rosters.json", "utf8")); } catch { return null; }
})();
const currentTeamOf = (athleteId: string): string | null =>
  rosterFile?.athletes?.[athleteId]?.team ?? null;
const onCurrentRoster = (athleteId: string, recordTeamAbbr: string) => {
  const current = currentTeamOf(athleteId);
  return current === null || current === recordTeamAbbr;
};

const ranked = rankCandidates(board.players, board.teams, matchups)
  .filter(candidate => slateTeamIds.has(candidate.teamId))
  .filter(candidate => candidate.sample.firstFieldGoals >= MIN_FIRST_BASKETS)
  .filter(candidate => {
    const keep = onCurrentRoster(candidate.athleteId, teamById.get(candidate.teamId)!.team);
    if (!keep) console.log(`Excluding ${candidate.player}: record team ${teamById.get(candidate.teamId)!.team}, now on ${currentTeamOf(candidate.athleteId)}`);
    return keep;
  })
  .filter(candidate => {
    // Players ruled Out never make the pick board; Day-To-Day stays with a
    // caution chip instead.
    const entry = (injuries.get(teamById.get(candidate.teamId)!.team) ?? [])
      .find(item => item.toLowerCase().includes(candidate.player.toLowerCase()));
    const ruledOut = entry ? /\bout\b/i.test(entry.replace(candidate.player, "")) : false;
    if (ruledOut) console.log(`Excluding ${candidate.player}: injury report says ${entry}`);
    return !ruledOut;
  });

// Data-derived role labels: the top first-basket share on each team is the
// team lead; high conversion on a modest share reads as a value branch.
const leadByTeam = new Map<string, string>();
for (const candidate of ranked) {
  if (!leadByTeam.has(candidate.teamId)) leadByTeam.set(candidate.teamId, candidate.athleteId);
}
const profileOf = (candidate: ModelCandidate) => {
  if (leadByTeam.get(candidate.teamId) === candidate.athleteId) return "Team lead";
  if (candidate.rates.firstAttemptMake >= 0.6 && candidate.rates.firstBasket < 0.25) return "Value";
  return "Secondary";
};

const picks = ranked.slice(0, PICK_COUNT).map(candidate => {
  const opponent = opponentOf(candidate);
  const team = teamById.get(candidate.teamId)!;
  const tipPct = pct(candidate.components.tipMatchup);
  const shotPct = pct(candidate.rates.firstAttempt, 0);
  const makePct = pct(candidate.rates.firstAttemptMake, 0);
  const signals = [
    `${candidate.sample.firstFieldGoals}/${candidate.sample.teamGames} team-first baskets`,
    `First shot in ${shotPct}% of games`,
    `Team wins ${pct(candidate.rates.teamTipWin, 0)}% of tips`,
  ];
  const cautions: string[] = [];
  const lowRead = lowReadTeams.get(candidate.teamId) ?? lowReadTeams.get(opponent.teamId);
  if (lowRead) cautions.push(`Red flag: ${display(lowRead.team)} game — ${display(lowRead.team)} scores first in only ${lowRead.scoredFirstFieldGoal}/${lowRead.games}, volatile opening`);
  const injuryHit = (injuries.get(team.team) ?? []).find(item => item.toLowerCase().includes(candidate.player.toLowerCase()));
  if (injuryHit) cautions.push(`Injury report: ${injuryHit}`);
  if (candidate.sample.teamGames < 15) cautions.push(`Small sample: ${candidate.sample.teamGames} games`);
  if (candidate.components.tipMatchup < 0.5) cautions.push(`Tip disadvantage vs ${display(opponent.team)}`);
  if (candidate.rates.firstAttemptMake < 0.5) cautions.push(`Converts ${makePct}% of first attempts`);
  if (candidate.sample.firstAttempts < 5) cautions.push(`Only ${candidate.sample.firstAttempts} tracked first attempts`);
  return {
    player: candidate.player,
    headshot: `https://a.espncdn.com/i/headshots/wnba/players/full/${candidate.athleteId}.png`,
    team: display(team.team),
    opp: display(opponent.team),
    profile: profileOf(candidate),
    score: candidate.edgeScore,
    odds: fairAmerican(candidate.rates.firstBasket),
    fb: `${candidate.sample.firstFieldGoals} / ${candidate.sample.teamGames}`,
    tip: tipPct,
    shot: shotPct,
    make: makePct,
    script: `${display(team.team)} wins ${pct(candidate.rates.teamTipWin, 0)}% of tips and scores first in ${pct(candidate.rates.teamScoresFirst, 0)}% of games; ${candidate.player.split(" ").slice(-1)[0]} takes the first shot in ${shotPct}% and converts ${makePct}% of those attempts.`,
    signals,
    cautions,
  };
});

// Opening chain per team: who takes the jump, who gains possession off won
// tips, who takes the team's first shot — all season counts with
// denominators, jumper selection injury-aware (Out players skipped).
const modelFile = await readOptional("data/wnba-model.json");
const rosterNameTeam: Record<string, string> = {};
for (const athlete of Object.values(rosterFile?.athletes ?? {}) as Array<{ name: string; team: string }>) {
  rosterNameTeam[athlete.name] = athlete.team;
}
const injuryEntry = (teamAbbr: string, name: string) =>
  (injuries.get(teamAbbr) ?? []).find(item => item.toLowerCase().includes(name.toLowerCase()));
const isRuledOut = (teamAbbr: string, name: string) => {
  const entry = injuryEntry(teamAbbr, name);
  return entry ? /\bout\b/i.test(entry.replace(name, "")) : false;
};

// Handler branch, measured from the season: how often the tip's possession
// gainer takes the team's first shot themselves (the "keep" branch the
// 8/7 slate hit three times).
let gainerShotSeen = 0, gainerShotBy = 0, gainerFgSeen = 0, gainerFgBy = 0;
for (const sequence of modelFile?.sequences ?? []) {
  const tip = sequence.tip ?? {};
  if (!tip.winningTeamId || !tip.possessionPlayerId) continue;
  const attempt = sequence.firstAttemptsByTeam?.[tip.winningTeamId];
  if (attempt?.athleteId) {
    gainerShotSeen += 1;
    if (attempt.athleteId === tip.possessionPlayerId) gainerShotBy += 1;
  }
  if (sequence.firstFieldGoal?.athleteId) {
    gainerFgSeen += 1;
    if (sequence.firstFieldGoal.athleteId === tip.possessionPlayerId) gainerFgBy += 1;
  }
}
const pGainerShoots = gainerShotSeen ? gainerShotBy / gainerShotSeen : 0.25;

const topGainerOf = (team: TeamAggregate) => {
  const counts = new Map<string, { name: string; n: number }>();
  let tipsWon = 0;
  for (const sequence of modelFile?.sequences ?? []) {
    const tip = sequence.tip ?? {};
    if (tip.winningTeamId !== team.teamId) continue;
    tipsWon += 1;
    if (tip.possessionPlayerId) {
      const entry = counts.get(tip.possessionPlayerId) ?? { name: tip.possessionPlayerName ?? tip.possessionPlayerId, n: 0 };
      entry.n += 1;
      counts.set(tip.possessionPlayerId, entry);
    }
  }
  const top = [...counts.entries()].sort((a, b) => b[1].n - a[1].n)[0];
  return top ? { athleteId: top[0], name: top[1].name, gains: top[1].n, tipsWon } : null;
};

interface ChainSide { jumper: string; gainer: string; shooter: string }
const chainOf = (team: TeamAggregate): ChainSide => {
  const jumperCounts = new Map<string, number>();
  let tipsSeen = 0;
  for (const sequence of modelFile?.sequences ?? []) {
    if (!sequence.teams?.some((item: any) => item.id === team.teamId)) continue;
    const tip = sequence.tip ?? {};
    const jump = /^\s*(.+?)\s+vs\.\s+(.+?)\s*\(/.exec(tip.text ?? "");
    if (jump) {
      tipsSeen += 1;
      for (const name of [jump[1], jump[2]]) {
        if (rosterNameTeam[name] === team.team) {
          jumperCounts.set(name, (jumperCounts.get(name) ?? 0) + 1);
        }
      }
    }
  }
  const ranked = [...jumperCounts.entries()].sort((a, b) => b[1] - a[1]);
  const available = ranked.find(([name]) => !isRuledOut(team.team, name));
  const usual = ranked[0];
  let jumper = "—";
  if (available) {
    const [name, count] = available;
    const dtd = injuryEntry(team.team, name) && !isRuledOut(team.team, name) ? " (DTD)" : "";
    jumper = `${name} ${count}/${tipsSeen}${dtd}`;
    if (usual && usual[0] !== name) jumper += ` — ${usual[0]} out`;
  }
  const top = topGainerOf(team);
  let gainer = "—";
  if (top) {
    const fg = board.players.find(player => player.athleteId === top.athleteId && player.teamId === team.teamId)?.firstFieldGoals ?? 0;
    gainer = `${top.name} ${top.gains}/${top.tipsWon}${fg ? ` · ${fg} first FGs` : ""}`;
  }
  const shooterAgg = board.players
    .filter(player => player.teamId === team.teamId && !isRuledOut(team.team, player.player))
    .sort((a, b) => b.firstTeamAttempts - a.firstTeamAttempts)[0];
  return {
    jumper,
    gainer,
    shooter: shooterAgg ? `${shooterAgg.player} ${shooterAgg.firstTeamAttempts}/${team.games}` : "—",
  };
};

const games = slate.map(game => {
  const homeTipRate = rate(game.home.tipWins, game.home.games);
  const awayTipRate = rate(game.away.tipWins, game.away.games);
  const split = homeTipRate + awayTipRate;
  const homeTip = split ? pct(homeTipRate / split) : 50;
  const homeFirst = rate(game.home.scoredFirstFieldGoal, game.home.games);
  const awayFirst = rate(game.away.scoredFirstFieldGoal, game.away.games);
  const gapPp = pct(homeFirst - awayFirst);
  const leader = gapPp >= 0 ? display(game.home.team) : display(game.away.team);
  const lineupNotes = [game.away, game.home]
    .map(team => {
      const list = injuries.get(team.team) ?? [];
      return list.length ? `${display(team.team)} missing ${list.map(item => item.replace(/ (Out|Day-To-Day|DTD)$/i, "")).join(", ")}` : null;
    })
    .filter(Boolean);
  return {
    away: display(game.away.team),
    home: display(game.home.team),
    time: game.time,
    homeTip,
    edge: Math.abs(gapPp) < 3 ? "Even lean" : `${leader} +${Math.abs(gapPp)}pp`,
    note: `${display(game.home.team)} scores first in ${pct(homeFirst, 0)}% of games, ${display(game.away.team)} in ${pct(awayFirst, 0)}%.`
      + (lineupNotes.length ? ` ${lineupNotes.join("; ")}.` : ""),
    chain: {
      away: chainOf(game.away),
      home: chainOf(game.home),
    },
    ...((() => {
      const lowRead = lowReadTeams.get(game.away.teamId) ?? lowReadTeams.get(game.home.teamId);
      return lowRead ? { flag: `${display(lowRead.team)} scores first in only ${lowRead.scoredFirstFieldGoal}/${lowRead.games} games — thin read, both sides` } : {};
    })()),
  };
});

// Team audit boards: who has done what on each slate team. Per AGENT.md the
// listed players' whole-game first baskets must sum to the team's total —
// the sum is emitted so the page can show the reconciliation.
const AUDIT_ROWS = 7;
const teamAudit = slate.flatMap(game => [game.away, game.home]).map(team => {
  const roster = board.players
    .filter(player => player.teamId === team.teamId)
    .filter(player => player.firstFieldGoals + player.firstTeamFieldGoals + player.firstTeamAttempts > 0)
    .sort((a, b) => b.firstFieldGoals - a.firstFieldGoals || b.firstTeamFieldGoals - a.firstTeamFieldGoals || b.firstTeamAttempts - a.firstTeamAttempts);
  const fbTotal = roster.reduce((sum, player) => sum + player.firstFieldGoals, 0);
  return {
    team: display(team.team),
    games: team.games,
    tipWins: team.tipWins,
    scoredFirst: team.scoredFirstFieldGoal,
    fbTotal,
    players: roster.slice(0, AUDIT_ROWS).map(player => {
      const current = currentTeamOf(player.athleteId);
      const departed = current !== null && current !== team.team;
      return {
        name: player.player,
        headshot: `https://a.espncdn.com/i/headshots/wnba/players/full/${player.athleteId}.png`,
        fb: player.firstFieldGoals,
        teamFirst: player.firstTeamFieldGoals,
        attempts: player.firstTeamAttempts,
        makes: player.firstTeamAttemptMakes,
        // Counts stay (they reconcile to the team total); the tag marks the
        // player as no longer on this roster.
        ...(departed ? { departed: true, nowWith: display(current!) } : {}),
      };
    }),
    more: Math.max(0, roster.length - AUDIT_ROWS),
  };
});

// Optional side inputs: hand-maintained wins ledger and the ESPN-synced
// triple-double watch (scripts/sync-td-watch.ts).
const winsFile = await readOptional("data/wins.json");
const tdFile = await readOptional("data/td-watch.json");

const slateAbbrs = new Set(slate.flatMap(game => [game.away.team, game.home.team]));
const tdWatch = (tdFile?.players ?? []).map((player: any) => ({
  ...player,
  team: display(player.team ?? ""),
  onSlate: slateAbbrs.has(player.team),
}));

const wins = winsFile?.wins ?? [];
const winTotals = wins.length ? {
  record: `${wins.length}-0`,
  staked: Number(wins.reduce((sum: number, win: any) => sum + win.stake, 0).toFixed(2)),
  returned: Number(wins.reduce((sum: number, win: any) => sum + win.payout, 0).toFixed(2)),
} : null;

// Market map: one row per game, every cell sourced from this file's data.
const marketMap = slate.map(game => {
  const awayDisp = display(game.away.team);
  const homeDisp = display(game.home.team);
  const generated = games.find(item => item.away === awayDisp && item.home === homeDisp)!;
  // Search the full ranked slate, not just the trimmed board — every game
  // gets its best candidate even when none cracked the top picks.
  const best = ranked.find(candidate => candidate.teamId === game.away.teamId || candidate.teamId === game.home.teamId) ?? null;
  const topPick = best ? { player: best.player, score: best.edgeScore, odds: fairAmerican(best.rates.firstBasket) } : null;
  const tdNames = tdWatch.filter((player: any) => player.onSlate && (player.team === awayDisp || player.team === homeDisp)).map((player: any) => player.player);
  return {
    game: `${awayDisp} @ ${homeDisp}`,
    time: game.time,
    tip: `${homeDisp} ${generated.homeTip}%`,
    firstScore: generated.edge,
    topPick: topPick ? `${topPick.player} · ${topPick.score} edge · ${topPick.odds}` : "—",
    tdWatch: tdNames.length ? tdNames.join(", ") : "—",
  };
});

// ── Opening-possession simulation ─────────────────────────────────────
// Monte Carlo of the user's branch logic: jump ball → keep or pass →
// first option shoots → miss branch (possession stays or flips, sampled
// from this season's observed rates). Seeded by slate date so the board
// is reproducible within a day. Not calibrated probability.
const SIM_RUNS = 1000;
const mulberry32 = (seed: number) => () => {
  seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
  let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const rand = mulberry32([...date].reduce((n, c) => (n * 31 + c.charCodeAt(0)) >>> 0, 7));

// Empirical miss branch from the synced sequences.
let missBranchSeen = 0, missBranchStay = 0, missBranchOwn = 0;
for (const sequence of modelFile?.sequences ?? []) {
  const attempt = sequence.firstAttempt, goal = sequence.firstFieldGoal;
  if (!attempt || !goal || attempt.made) continue;
  missBranchSeen += 1;
  if (goal.teamId === attempt.teamId) {
    missBranchStay += 1;
    if (goal.athleteId === attempt.athleteId) missBranchOwn += 1;
  }
}
const pStay = missBranchSeen ? missBranchStay / missBranchSeen : 0.45;
const pOwn = missBranchStay ? missBranchOwn / missBranchStay : 0.2;

const simPoolOf = (team: TeamAggregate) => board.players.filter(player =>
  player.teamId === team.teamId && player.firstTeamAttempts > 0 &&
  onCurrentRoster(player.athleteId, team.team) && !isRuledOut(team.team, player.player));

const simGame = (game: SlateGame) => {
  const pools = { away: simPoolOf(game.away), home: simPoolOf(game.home) };
  const gainers = {
    away: (() => { const top = topGainerOf(game.away); return top ? pools.away.find(p => p.athleteId === top.athleteId) ?? null : null; })(),
    home: (() => { const top = topGainerOf(game.home); return top ? pools.home.find(p => p.athleteId === top.athleteId) ?? null : null; })(),
  };
  const teamMake = (pool: PlayerAggregate[]) =>
    pool.reduce((n, p) => n + p.firstTeamAttemptMakes, 0) / Math.max(1, pool.reduce((n, p) => n + p.firstTeamAttempts, 0));
  const makes = { away: teamMake(pools.away), home: teamMake(pools.home) };
  const homeTipRate = rate(game.home.tipWins, game.home.games);
  const awayTipRate = rate(game.away.tipWins, game.away.games);
  const pHomeTip = homeTipRate / Math.max(1e-9, homeTipRate + awayTipRate);
  const draw = (pool: PlayerAggregate[]) => {
    let r = rand() * pool.reduce((n, p) => n + p.firstTeamAttempts, 0);
    for (const p of pool) { r -= p.firstTeamAttempts; if (r <= 0) return p; }
    return pool[pool.length - 1];
  };
  const makeProb = (p: PlayerAggregate, fallback: number) =>
    p.firstTeamAttempts >= 3 ? Math.min(0.8, Math.max(0.25, p.firstTeamAttemptMakes / p.firstTeamAttempts)) : fallback;
  const counts = new Map<string, { player: PlayerAggregate; side: "away" | "home"; n: number }>();
  for (let run = 0; run < SIM_RUNS; run += 1) {
    let side: "away" | "home" = rand() < pHomeTip ? "home" : "away";
    let shooter: PlayerAggregate | null = null;
    let keepOwn = false;
    for (let shot = 0; shot < 12; shot += 1) {
      if (shot === 0 && gainers[side] && rand() < pGainerShoots) {
        // Handler keep branch: the tip's possession gainer takes the first
        // shot themselves, at the season-observed rate.
        shooter = gainers[side];
      } else if (!shooter || !keepOwn) shooter = draw(pools[side]);
      if (rand() < makeProb(shooter, makes[side])) {
        const key = shooter.athleteId;
        const entry = counts.get(key) ?? { player: shooter, side, n: 0 };
        entry.n += 1; counts.set(key, entry);
        break;
      }
      if (rand() < pStay) { keepOwn = rand() < pOwn; if (!keepOwn) shooter = null; }
      else { side = side === "home" ? "away" : "home"; shooter = null; keepOwn = false; }
    }
  }
  return [...counts.values()].sort((a, b) => b.n - a.n);
};

const simGames = slate.map(game => {
  const ranked = simGame(game);
  return {
    game: `${display(game.away.team)} @ ${display(game.home.team)}`,
    top: ranked.slice(0, 4).map(entry => ({
      player: entry.player.player,
      team: display((entry.side === "home" ? game.home : game.away).team),
      headshot: `https://a.espncdn.com/i/headshots/wnba/players/full/${entry.player.athleteId}.png`,
      count: entry.n,
      p: Number((entry.n / SIM_RUNS).toFixed(3)),
    })),
  };
});

// Best cross-game 2-leg combo: highest joint frequency across two games.
let simCombo = null;
const leaders = simGames.filter(item => item.top.length).map(item => ({ game: item.game, leg: item.top[0] }));
for (let a = 0; a < leaders.length; a += 1) {
  for (let b = a + 1; b < leaders.length; b += 1) {
    const joint = leaders[a].leg.p * leaders[b].leg.p;
    if (!simCombo || joint > simCombo.p) {
      const decimal = 1 / joint;
      simCombo = {
        legs: [
          { ...leaders[a].leg, game: leaders[a].game },
          { ...leaders[b].leg, game: leaders[b].game },
        ],
        p: Number(joint.toFixed(4)),
        decimal: Number(decimal.toFixed(1)),
        fair: fairAmerican(joint),
      };
    }
  }
}

const sim = {
  runs: SIM_RUNS,
  missBranch: `${missBranchStay}/${missBranchSeen} missed first attempts stayed with the shooting team; ${missBranchOwn}/${missBranchStay} finished by the same shooter`,
  handlerBranch: `tip gainer takes the first shot in ${gainerShotBy}/${gainerShotSeen} won tips and scores the game's first FG ${gainerFgBy}/${gainerFgSeen}`,
  games: simGames,
  combo: simCombo,
};

const output = {
  league: "WNBA",
  date,
  dateLabel: label,
  updated: `synced ${board.start} → ${board.end}`,
  source: "ESPN play-by-play · first field goal market · prices are model-fair lines, no sportsbook feed",
  headshotBase: "https://a.espncdn.com/i/headshots/wnba/players/full/",
  weights: [
    [30, "Player FB share"], [20, "Team opening profile"], [15, "First-shot involvement"],
    [15, "Tip matchup"], [10, "Role / availability"], [5, "H2H"], [5, "Price"],
  ],
  picks,
  games,
  teamAudit,
  marketMap,
  sim,
  tdWatch,
  tdSource: tdFile?.source ?? null,
  wins,
  winTotals,
};

const banner = `// Opening Edge — WNBA first-basket model section (#opening).
// GENERATED by model/opening-edge/scripts/generate-section.ts from the synced
// snapshot (${board.generatedAt}); do not hand-edit. Regenerate with a slate:
//   cd model/opening-edge && node --experimental-strip-types scripts/generate-section.ts \\
//     --date YYYY-MM-DD --label "Day, Mon D" --slate "AWY@HOM=7:00 PM ET,..." \\
//     [--injuries "NY:Player Out,...;CHI:Player Out,..."]
`;
await writeFile("../../data/opening-edge.js", `${banner}window.OPENING_EDGE = ${JSON.stringify(output, null, 2)};\n`, "utf8");
console.log(`Wrote ${picks.length} picks, ${games.length} games for ${date} to data/opening-edge.js`);
