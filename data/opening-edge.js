// Opening Edge — WNBA first-basket model section data.
// Hand-authored from the Opening Edge model slate; NOT produced by the board
// pipeline, so snapshot refreshes never touch this file.
// Headshots are official WNBA CDN cutouts keyed by wnba.com person id.
window.OPENING_EDGE = {
  league: "WNBA",
  date: "2026-08-05",
  dateLabel: "Wednesday, Aug 5",
  updated: "11:42 AM",
  headshotBase: "https://cdn.wnba.com/headshots/wnba/latest/1040x760/",
  weights: [
    [30, "Player FB share"],
    [20, "Team opening profile"],
    [15, "First-shot involvement"],
    [15, "Tip matchup"],
    [10, "Role / availability"],
    [5, "H2H"],
    [5, "Price"],
  ],
  picks: [
    {
      player: "Shakira Austin", wnbaId: 1631022, team: "WAS", opp: "DAL",
      profile: "Lead Big", score: 92, odds: "+360",
      fb: "8 / 16", tip: 73.1, shot: 42, make: 66,
      script: "Washington controls the tap, enters early, and Austin seals before Dallas can load the paint.",
      signals: ["Team-best 8 whole-game first baskets", "17–7 jump-ball matchup", "Dallas only 28.6% on tips"],
      cautions: ["Short price limits parlay value"],
    },
    {
      player: "Jonquel Jones", wnbaId: 1627673, team: "NYL", opp: "SEA",
      profile: "Lead Big", score: 88, odds: "+470",
      fb: "4 / 16", tip: 58.6, shot: 36, make: 61,
      script: "Jones wins the middle and New York flows directly into a rim touch or put-back chance.",
      signals: ["Liberty lead profile", "17–10 on opening jumps", "Paint touch survives a miss"],
      cautions: ["Only 4 tracked first makes"],
    },
    {
      player: "Rhyne Howard", wnbaId: 1631009, team: "ATL", opp: "PHX",
      profile: "Lead Guard", score: 85, odds: "+550",
      fb: "5 / 11", tip: 61.5, shot: 44, make: 47,
      script: "Atlanta wins possession and Howard gets the scripted above-the-break look before Phoenix settles.",
      signals: ["5 of Atlanta’s 11 first baskets", "Home first-score rate 69.2%", "Highest guard share on slate"],
      cautions: ["Jump-shot dependent (47 make idx)"],
    },
    {
      player: "Nneka Ogwumike", wnbaId: 203014, team: "LA", opp: "CHI",
      profile: "Primary", score: 82, odds: "+500",
      fb: "7 / 14", tip: 42.3, shot: 39, make: 59,
      script: "Even without the tap, Los Angeles’ first organized possession bends toward Nneka at the elbow.",
      signals: ["Half of LA’s tracked first baskets", "Reliable first-set target", "Second-chance path"],
      cautions: ["LA loses the tap more often than not"],
    },
    {
      player: "Sabrina Ionescu", wnbaId: 1629477, team: "NYL", opp: "SEA",
      profile: "Lead Guard", score: 78, odds: "+625",
      fb: "2 / 16", tip: 58.6, shot: 41, make: 44,
      script: "If Jones does not finish the first paint touch, Sabrina relocates into the first clean three or attacks downhill.",
      signals: ["Green-light secondary branch", "Return-from-injury aggression", "Pairs cleanly with Austin"],
      cautions: ["Only 2 tracked first baskets", "Second option behind Jones"],
    },
    {
      player: "Dominique Malonga", wnbaId: 1642798, team: "SEA", opp: "NYL",
      profile: "Value", score: 73, odds: "+650",
      fb: "5 / 16", tip: 55.2, shot: 32, make: 63,
      script: "Seattle’s upset branch: Malonga steals the tap and outruns New York into the first deep catch.",
      signals: ["Seattle co-leader: 5 first baskets", "High rim conversion", "Plus-money hedge to Jones"],
      cautions: ["Underdog on the opening jump"],
    },
  ],
  games: [
    { away: "DAL", home: "WAS", time: "7:30 PM ET", homeTip: 73.1, edge: "WAS +44.5", note: "Largest tap edge on the board." },
    { away: "SEA", home: "NYL", time: "7:00 PM ET", homeTip: 58.6, edge: "NYL +3.4", note: "New York has the stronger second action." },
    { away: "PHX", home: "ATL", time: "7:30 PM ET", homeTip: 61.5, edge: "ATL +15.1", note: "Atlanta owns the cleaner script." },
    { away: "LA", home: "CHI", time: "8:00 PM ET", homeTip: 57.7, edge: "CHI lean", note: "Player role matters more here." },
  ],
};
