(() => {
  "use strict";

  const SPORT_META = {
    mlb: { label: "MLB", accent: "#ff7777" },
    nba: { label: "NBA", accent: "#6eb8ff" },
    wnba: { label: "WNBA", accent: "#ffae70" },
    soccer: { label: "Soccer", accent: "#65e0b1" },
    tennis: { label: "Tennis", accent: "#cba8ff" },
    nfl: { label: "NFL", accent: "#7dde77" },
  };

  const DIMENSIONS = [
    { key: "strength", label: "Model", weight: 1.2 },
    { key: "probability", label: "Probability", weight: 1.1 },
    { key: "value", label: "Price value", weight: 1.25 },
    { key: "conviction", label: "Conviction", weight: 0.85 },
    { key: "coherence", label: "Coherence", weight: 0.6 },
  ];

  const app = document.getElementById("app");
  const nav = document.getElementById("primary-nav");
  const mobileNav = document.getElementById("mobile-sport-nav");
  const headerFreshness = document.getElementById("header-freshness");
  const drawer = document.getElementById("signal-drawer");
  const drawerContent = document.getElementById("drawer-content");
  const drawerBackdrop = document.getElementById("drawer-backdrop");
  const drawerClose = document.getElementById("drawer-close");
  const toast = document.getElementById("toast");

  const filters = {};
  const limits = {};
  let snapshot = null;
  let rows = [];
  let rowMap = new Map();
  let games = [];
  let gameSport = "mlb";
  let renderQueued = false;
  let searchTimer = null;
  let saved = readSaved();

  const clamp = (value, min = 0, max = 1) => Math.min(max, Math.max(min, Number(value) || 0));
  const esc = value => String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");

  const slug = value => String(value ?? "unknown")
    .normalize("NFKD")
    .replace(/[^a-zA-Z0-9.]+/g, "-")
    .replace(/^-|-$/g, "")
    .toLowerCase();

  function readSaved() {
    try { return JSON.parse(localStorage.getItem("board-signal-card") || "[]"); }
    catch { return []; }
  }

  function writeSaved() {
    localStorage.setItem("board-signal-card", JSON.stringify(saved));
  }

  function americanToProbability(value) {
    if (value === null || value === undefined || value === "") return null;
    const odds = Number(String(value).replace("+", ""));
    if (!Number.isFinite(odds) || odds === 0) return null;
    return odds > 0 ? 100 / (odds + 100) : Math.abs(odds) / (Math.abs(odds) + 100);
  }

  function parseEvidence(reason = "") {
    const text = String(reason);
    const capture = (regex, group = 1) => {
      const match = text.match(regex);
      return match ? match[group] : null;
    };
    const number = regex => {
      const value = capture(regex);
      return value === null ? null : Number(value);
    };
    return {
      projection: number(/\bProj\s+([0-9.]+)/i),
      baseline: number(/\bBaseline\s+([0-9.]+)/i),
      l5: capture(/\bL5\s+([^|]+)/i)?.trim() || null,
      l10: capture(/\bL10\s+([^|]+)/i)?.trim() || null,
      matchup: capture(/\b(?:AST|REB|PTS|3PM)?\s*matchup\s+([0-9.]+x?)/i),
      h2h: capture(/\bH2H\s+([^|]+)/i)?.trim() || null,
      usage: capture(/\bUSG\s+([0-9.]+%)/i),
      minutes: number(/\bMIN\s+([0-9.]+)/i),
      sample: number(/\bSample\s+([0-9.]+)/i),
      lineup: capture(/\bStatus\s+([A-Z]+)/i),
    };
  }

  function thresholdFromLine(line = "") {
    const match = String(line).match(/([0-9]+(?:\.[0-9]+)?)/);
    return match ? Number(match[1]) : null;
  }

  function percentile(value, values) {
    const clean = values.filter(Number.isFinite);
    if (!Number.isFinite(value) || !clean.length) return null;
    if (clean.length === 1) return 0.78;
    let less = 0;
    let equal = 0;
    clean.forEach(item => {
      if (item < value) less += 1;
      else if (item === value) equal += 1;
    });
    return (less + equal * 0.5) / clean.length;
  }

  function flattenSnapshot(data) {
    const output = [];
    const eventOutput = [];
    Object.entries(data.sports || {}).forEach(([sport, board]) => {
      if (!board || !Array.isArray(board.games)) return;
      board.games.forEach(game => {
        const event = {
          sport,
          sportLabel: SPORT_META[sport]?.label || sport.toUpperCase(),
          date: board.date,
          lastUpdated: board.last_updated,
          gameId: String(game.game_id || slug(`${board.date}-${game.matchup}`)),
          matchup: game.matchup || "Matchup pending",
          time: game.time || game.status || "TBD",
          status: game.status || "scheduled",
          env: game.env || null,
          rows: [],
        };
        Object.entries(game.markets || {}).forEach(([market, selections]) => {
          (selections || []).forEach(selection => {
            const probability = selection.sim_prob_pct !== null && selection.sim_prob_pct !== undefined
              ? Number(selection.sim_prob_pct) / 100
              : selection.model_hit_rate !== null && selection.model_hit_rate !== undefined
                ? Number(selection.model_hit_rate)
                : null;
            const impliedProbability = americanToProbability(selection.implied_odds ?? selection.book_odds);
            const evidence = parseEvidence(selection.reason);
            const threshold = thresholdFromLine(selection.line);
            const id = [sport, event.gameId, selection.player_id || selection.player_name, market, selection.line]
              .map(slug).join(":");
            const row = {
              id,
              sport,
              sportLabel: event.sportLabel,
              date: board.date,
              lastUpdated: board.last_updated,
              gameId: event.gameId,
              matchup: event.matchup,
              time: event.time,
              market,
              playerId: String(selection.player_id || ""),
              playerName: selection.player_name || selection.team || "Unknown",
              team: selection.team || "—",
              opponent: selection.opponent || "—",
              line: selection.line || market,
              score: Number(selection.score || 0),
              confidence: selection.confidence !== null && selection.confidence !== undefined
                ? Number(selection.confidence) / 100
                : ({ A: .88, B: .7, C: .52, D: .35 }[String(selection.tier || "C").toUpperCase()] || .5),
              tier: String(selection.tier || "C").toUpperCase(),
              probability,
              impliedOdds: selection.implied_odds ?? selection.book_odds ?? null,
              impliedProbability,
              priceEdge: probability !== null && impliedProbability !== null ? probability - impliedProbability : null,
              projectionDelta: evidence.projection !== null && threshold !== null ? evidence.projection - threshold : null,
              threshold,
              evidence,
              reason: selection.reason || "No model note supplied.",
              raw: selection,
            };
            output.push(row);
            event.rows.push(row);
          });
        });
        eventOutput.push(event);
      });
    });
    return { rows: dedupe(output), games: eventOutput };
  }

  function dedupe(items) {
    const seen = new Set();
    return items.filter(item => {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    });
  }

  function applyGeometry(allRows) {
    const groups = new Map();
    allRows.forEach(row => {
      const key = `${row.sport}:${row.market}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    });

    allRows.forEach(row => {
      const peers = groups.get(`${row.sport}:${row.market}`) || [row];
      const scoreRank = percentile(row.score, peers.map(item => item.score));
      const probabilityRank = row.probability === null
        ? null
        : percentile(row.probability, peers.map(item => item.probability).filter(value => value !== null));
      const strength = clamp(.12 + .88 * (scoreRank ?? .5), .08, .99);
      const probability = probabilityRank === null ? null : clamp(.12 + .88 * probabilityRank, .08, .99);
      const value = row.priceEdge === null ? null : clamp(.5 + row.priceEdge * 3.2, .05, .98);
      const conviction = clamp(row.confidence, .08, .99);
      const coherence = probability === null ? null : clamp(1 - Math.abs(strength - probability) * .85, .12, .99);
      const vector = { strength, probability, value, conviction, coherence };
      const available = DIMENSIONS.filter(dimension => vector[dimension.key] !== null);
      const totalWeight = DIMENSIONS.reduce((sum, dimension) => sum + dimension.weight, 0);
      const availableWeight = available.reduce((sum, dimension) => sum + dimension.weight, 0);
      const distance = Math.sqrt(
        available.reduce((sum, dimension) => sum + dimension.weight * ((1 - vector[dimension.key]) ** 2), 0) /
        Math.max(availableWeight, .001)
      );
      const idealProximity = clamp(1 - distance);
      const geometricBalance = Math.exp(
        available.reduce((sum, dimension) => sum + dimension.weight * Math.log(Math.max(.04, vector[dimension.key])), 0) /
        Math.max(availableWeight, .001)
      );
      const coverage = availableWeight / totalWeight;
      let penalty = 1;
      const flags = [];
      if (row.priceEdge !== null && row.priceEdge < -.025) {
        penalty *= .92;
        flags.push("Price below model value threshold");
      }
      if (row.projectionDelta !== null && row.projectionDelta < 0) {
        penalty *= .94;
        flags.push("Projection sits below the offered line");
      }
      if (row.evidence.sample !== null && row.evidence.sample < .35) {
        penalty *= .96;
        flags.push("Thin supporting sample");
      }
      const geometry = Math.round(100 * (.55 * idealProximity + .45 * geometricBalance) * (.64 + .36 * coverage) * penalty);
      Object.assign(row, {
        vector,
        geometry: Math.max(1, Math.min(99, geometry)),
        coverage,
        idealProximity,
        geometricBalance,
        flags,
      });
      row.verdict = verdictFor(row);
    });
  }

  function verdictFor(row) {
    if (row.priceEdge !== null && row.priceEdge < -.025) return { label: "Price conflict", tone: "negative" };
    if (row.projectionDelta !== null && row.projectionDelta < 0) return { label: "Line conflict", tone: "negative" };
    if (row.geometry >= 74 && row.coverage >= .7 && row.priceEdge !== null && row.priceEdge >= 0) {
      return { label: "Qualified", tone: "positive" };
    }
    if (row.geometry >= 62) return { label: row.coverage < .62 ? "Model only" : "Watch", tone: "neutral" };
    return { label: "Pass", tone: "neutral" };
  }

  function diversifiedTop(source, count = 5, maxPerSport = 2) {
    const verdictPriority = { Qualified: 4, Watch: 3, "Model only": 2, Pass: 1 };
    const sorted = [...source]
      .filter(row => row.verdict.label !== "Price conflict" && row.verdict.label !== "Line conflict")
      .sort((a, b) => (verdictPriority[b.verdict.label] || 0) - (verdictPriority[a.verdict.label] || 0) || b.geometry - a.geometry || b.score - a.score);
    const result = [];
    const sportCounts = new Map();
    const players = new Set();
    for (const row of sorted) {
      if ((sportCounts.get(row.sport) || 0) >= maxPerSport) continue;
      const identity = `${row.sport}:${row.playerId || row.playerName}:${row.market}`;
      if (players.has(identity)) continue;
      result.push(row);
      players.add(identity);
      sportCounts.set(row.sport, (sportCounts.get(row.sport) || 0) + 1);
      if (result.length >= count) break;
    }
    return result;
  }

  function radarPoints(values, radius = 42, center = 50) {
    return values.map((value, index) => {
      const angle = -Math.PI / 2 + index * (Math.PI * 2 / values.length);
      const r = radius * clamp(value ?? .28);
      return `${(center + Math.cos(angle) * r).toFixed(1)},${(center + Math.sin(angle) * r).toFixed(1)}`;
    }).join(" ");
  }

  function radar(row, large = false) {
    const values = DIMENSIONS.map(dimension => row.vector?.[dimension.key] ?? .26);
    const outer = radarPoints([1, 1, 1, 1, 1]);
    const inner = radarPoints([.58, .58, .58, .58, .58]);
    const field = radarPoints(values);
    const axes = radarPoints([1, 1, 1, 1, 1]).split(" ").map(point => {
      const [x, y] = point.split(",");
      return `<line class="radar-axis" x1="50" y1="50" x2="${x}" y2="${y}"></line>`;
    }).join("");
    return `<div class="radar-wrap${large ? " radar-large" : ""}" aria-label="Signal geometry ${row.geometry} out of 100">
      <svg viewBox="0 0 100 100" aria-hidden="true">
        <polygon class="radar-grid" points="${outer}"></polygon>
        <polygon class="radar-grid" points="${inner}"></polygon>
        ${axes}
        <polygon class="radar-field" points="${field}"></polygon>
      </svg>
      <div class="radar-score"><strong>${row.geometry}</strong><span>VECTOR</span></div>
    </div>`;
  }

  function latestSlateDate() {
    const dates = Object.values(snapshot?.sports || {}).map(board => board?.date).filter(Boolean).sort();
    return dates[dates.length - 1] || null;
  }

  function staleInfo(date = latestSlateDate()) {
    if (!date) return { stale: true, latest: "unknown", days: null };
    const slate = new Date(`${date}T12:00:00`);
    const now = new Date();
    const days = Math.floor((now - slate) / 86400000);
    return { stale: days > 1, latest: date, days };
  }

  function freshnessBanner(date = latestSlateDate()) {
    const freshness = staleInfo(date);
    if (!freshness.stale) return "";
    return `<div class="stale-banner"><span><strong>Historical snapshot</strong> · Latest slate is ${esc(freshness.latest)}. Rankings are valid for inspection, not a current recommendation.</span><span>${freshness.days ?? "?"}d old</span></div>`;
  }

  function tierToken(tier) {
    return `<span class="tier-token tier-${esc(String(tier).toLowerCase())}">${esc(tier)}</span>`;
  }

  function probabilityText(row) {
    return row.probability === null ? "—" : `${(row.probability * 100).toFixed(1)}%`;
  }

  function priceText(row) {
    if (row.impliedOdds === null || row.impliedOdds === undefined || row.impliedOdds === "") return "No price";
    const value = String(row.impliedOdds);
    return Number(value) > 0 && !value.startsWith("+") ? `+${value}` : value;
  }

  function edgeText(row) {
    if (row.priceEdge === null) return "Price unverified";
    const points = row.priceEdge * 100;
    return `${points >= 0 ? "+" : ""}${points.toFixed(1)}pp value`;
  }

  function factorSummary(row) {
    const pieces = [];
    if (row.probability !== null) pieces.push(`${probabilityText(row)} sim`);
    if (row.priceEdge !== null) pieces.push(edgeText(row));
    else pieces.push(`${Math.round(row.coverage * 100)}% coverage`);
    return pieces.join(" · ");
  }

  function signalCard(row) {
    return `<article class="signal-card${row.flags.length ? " flagged" : ""}" data-selection-id="${esc(row.id)}" tabindex="0" role="button" aria-label="Open ${esc(row.playerName)} ${esc(row.line)} analysis">
      <div class="signal-top">
        <span class="sport-token" data-sport="${esc(row.sport)}">${esc(row.sportLabel)} · ${esc(row.market)}</span>
        <span class="status-token ${esc(row.verdict.tone)}">${esc(row.verdict.label)}</span>
      </div>
      <div class="signal-main">
        <div>
          <h3 class="signal-name">${esc(row.playerName)}</h3>
          <div class="signal-meta">${esc(row.team)} vs ${esc(row.opponent)} · ${esc(row.time)}</div>
          <div class="signal-line">${esc(row.line)} <small>${esc(priceText(row))}</small></div>
        </div>
        ${radar(row)}
      </div>
      <div class="signal-footer">
        <span class="factor-summary">${esc(factorSummary(row))}</span>
        <span class="open-cue">Inspect →</span>
      </div>
    </article>`;
  }

  function listMarkup(source, limit = 20) {
    if (!source.length) return `<div class="empty-state"><strong>No signals survived these filters.</strong>Relax one filter or switch markets.</div>`;
    return `<div class="signal-list">
      <div class="list-head"><span>Selection</span><span>Market / line</span><span>Model</span><span>Sim</span><span>Vector</span><span>Verdict</span></div>
      ${source.slice(0, limit).map((row, index) => `<div class="signal-row" data-selection-id="${esc(row.id)}" tabindex="0" role="button" aria-label="Open ${esc(row.playerName)} ${esc(row.line)} analysis">
        <div class="row-person"><span class="row-rank">${String(index + 1).padStart(2, "0")}</span><div><strong>${esc(row.playerName)}</strong><span>${esc(row.team)} vs ${esc(row.opponent)} · ${esc(row.time)}</span></div></div>
        <div class="row-market"><span class="market-token">${esc(row.market)}</span><strong>${esc(row.line)}</strong></div>
        <span class="row-number">${row.score.toFixed(1)}</span>
        <span class="row-number ${row.priceEdge !== null && row.priceEdge < 0 ? "warn" : ""}">${esc(probabilityText(row))}</span>
        <span class="geometry-cell"><i class="mini-orbit" style="--score:${row.geometry}"></i>${row.geometry}</span>
        <span class="status-token ${esc(row.verdict.tone)}">${esc(row.verdict.label)}</span>
      </div>`).join("")}
    </div>`;
  }

  function metricsMarkup(source, eventCount) {
    const qualified = source.filter(row => row.geometry >= 74 && row.verdict.label === "Qualified").length;
    const priced = source.filter(row => row.priceEdge !== null).length;
    const avgCoverage = source.length ? source.reduce((sum, row) => sum + row.coverage, 0) / source.length : 0;
    const best = [...source].sort((a, b) => b.geometry - a.geometry)[0];
    return `<section class="metric-strip" aria-label="Slate summary">
      <div class="metric"><span class="metric-label">Qualified</span><strong>${qualified}</strong><p>balanced, conflict-free signals</p></div>
      <div class="metric"><span class="metric-label">Best vector</span><strong>${best?.geometry ?? "—"}</strong><p>${best ? esc(best.playerName) : "No selections"}</p></div>
      <div class="metric"><span class="metric-label">Price coverage</span><strong>${source.length ? Math.round(priced / source.length * 100) : 0}%</strong><p>odds + probability available</p></div>
      <div class="metric"><span class="metric-label">Signal coverage</span><strong>${Math.round(avgCoverage * 100)}%</strong><p>${eventCount} events in scope</p></div>
    </section>`;
  }

  function pageHead(eyebrow, title, muted, copy, date) {
    return `<header class="page-head">
      <div><div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}${muted ? ` <em>${esc(muted)}</em>` : ""}</h1>${copy ? `<p class="page-copy">${esc(copy)}</p>` : ""}</div>
      <div class="page-date"><strong>${esc(date || "Slate pending")}</strong><span>Geometry-ranked · market-relative</span></div>
    </header>`;
  }

  function renderToday() {
    const date = latestSlateDate();
    const activeRows = rows.filter(row => row.date === date);
    const activeGames = games.filter(game => game.date === date);
    const top = diversifiedTop(activeRows, 5);
    const next = [...activeRows]
      .filter(row => !top.some(item => item.id === row.id))
      .sort((a, b) => b.geometry - a.geometry || b.score - a.score)
      .slice(0, 10);
    return `${pageHead("Decision surface", "Read the field.", "Ignore the noise.", "The Board now promotes balanced signals—not the loudest raw score. Price conflicts, missing inputs, and projection gaps remain visible instead of being averaged away.", date)}
      ${freshnessBanner(date)}
      ${metricsMarkup(activeRows, activeGames.length)}
      <div class="section-head"><div><span class="section-kicker">Diversified top field</span><h2>Five signals worth opening</h2></div><p class="section-note">Maximum two per sport. Duplicate player-market combinations are removed before ranking.</p></div>
      <section class="lead-grid">${top.map(signalCard).join("")}</section>
      <div class="section-head"><div><span class="section-kicker">Next tier</span><h2>The edge queue</h2></div><p class="section-note">A single ranked surface replaces overlapping “top,” “safe,” “sim,” and research boards.</p></div>
      ${listMarkup(next, 10)}`;
  }

  function sportFilters(sport) {
    if (!filters[sport]) filters[sport] = { search: "", market: "", tier: "", sort: sport === "soccer" ? "probability" : "geometry" };
    return filters[sport];
  }

  function probabilityLeaders(source, count = 3) {
    const bestByMarket = new Map();
    [...source]
      .filter(row => row.probability !== null)
      .sort((a, b) => b.probability - a.probability || b.geometry - a.geometry)
      .forEach(row => {
        if (!bestByMarket.has(row.market)) bestByMarket.set(row.market, row);
      });
    return [...bestByMarket.values()].sort((a, b) => b.probability - a.probability).slice(0, count);
  }

  function filteredSportRows(sport) {
    const filter = sportFilters(sport);
    const source = rows.filter(row => row.sport === sport).filter(row => {
      if (filter.market && row.market !== filter.market) return false;
      if (filter.tier && row.tier !== filter.tier) return false;
      if (filter.search) {
        const haystack = [row.playerName, row.team, row.opponent, row.matchup, row.time, row.market, row.line].join(" ").toLowerCase();
        if (!haystack.includes(filter.search.toLowerCase())) return false;
      }
      return true;
    });
    source.sort((a, b) => {
      if (filter.sort === "model") return b.score - a.score;
      if (filter.sort === "probability") return (b.probability ?? -1) - (a.probability ?? -1);
      if (filter.sort === "time") return String(a.time).localeCompare(String(b.time));
      return b.geometry - a.geometry || b.score - a.score;
    });
    return source;
  }

  function renderSport(sport) {
    const meta = SPORT_META[sport] || { label: sport.toUpperCase() };
    const source = rows.filter(row => row.sport === sport);
    if (!source.length) return renderMissingSport(sport);
    const current = filteredSportRows(sport);
    const filter = sportFilters(sport);
    const markets = [...new Set(source.map(row => row.market))].sort();
    const sportGames = games.filter(game => game.sport === sport);
    const top = sport === "soccer" ? probabilityLeaders(current, 3) : diversifiedTop(current, 3, 3);
    const limit = limits[sport] || 25;
    const latest = snapshot.sports[sport]?.date || staleInfo().latest;
    return `${pageHead(`${meta.label} signal field`, meta.label, "market map.", "", latest)}
      ${freshnessBanner(latest)}
      ${metricsMarkup(source, sportGames.length)}
      <div class="section-head"><div><span class="section-kicker">Current profile</span><h2>${sport === "soccer" ? "Highest modeled probabilities" : "Best balanced signals"}</h2></div><p class="section-note">${sport === "soccer" ? "One leader per market; use the probability sort below for the full board." : "These cards exclude hard price and projection conflicts."}</p></div>
      <section class="lead-grid">${top.map(signalCard).join("")}</section>
      <div class="toolbar" aria-label="Board filters">
        <label class="field"><input data-filter="search" data-sport="${esc(sport)}" value="${esc(filter.search)}" placeholder="Search player, team, game"></label>
        <label class="field"><select data-filter="market" data-sport="${esc(sport)}"><option value="">All markets</option>${markets.map(market => `<option value="${esc(market)}"${filter.market === market ? " selected" : ""}>${esc(market)}</option>`).join("")}</select></label>
        <label class="field"><select data-filter="tier" data-sport="${esc(sport)}"><option value="">All tiers</option>${["A","B","C"].map(tier => `<option value="${tier}"${filter.tier === tier ? " selected" : ""}>Tier ${tier}</option>`).join("")}</select></label>
        <label class="field"><select data-filter="sort" data-sport="${esc(sport)}"><option value="geometry"${filter.sort === "geometry" ? " selected" : ""}>Vector score</option><option value="model"${filter.sort === "model" ? " selected" : ""}>Raw model</option><option value="probability"${filter.sort === "probability" ? " selected" : ""}>Sim probability</option><option value="time"${filter.sort === "time" ? " selected" : ""}>Game time</option></select></label>
      </div>
      ${listMarkup(current, limit)}
      ${current.length > limit ? `<button class="show-more" data-action="more" data-sport="${esc(sport)}">Show 25 more · ${current.length - limit} remain</button>` : ""}`;
  }

  function renderMissingSport(sport) {
    return `${pageHead("No active slate", SPORT_META[sport]?.label || sport.toUpperCase(), "is waiting.", "This sport does not have a populated board in the current snapshot.", staleInfo().latest)}<div class="empty-state"><strong>No event data was exported.</strong>Run the sport pipeline, then refresh this view.</div>`;
  }

  function renderGames() {
    const activeSports = Object.keys(SPORT_META).filter(sport => games.some(game => game.sport === sport));
    if (!activeSports.includes(gameSport)) gameSport = activeSports[0] || "mlb";
    const source = games.filter(game => game.sport === gameSport);
    const board = snapshot.sports[gameSport];
    return `${pageHead("Event index", "Games", "without the sprawl.", "Every event gets its own compact card. Open a signal only when the matchup earns a closer look.", board?.date || staleInfo().latest)}
      ${freshnessBanner(board?.date)}
      <div class="toolbar" style="grid-template-columns:minmax(180px,260px)">
        <label class="field"><select data-filter="game-sport">${activeSports.map(sport => `<option value="${esc(sport)}"${sport === gameSport ? " selected" : ""}>${esc(SPORT_META[sport]?.label || sport.toUpperCase())} · ${games.filter(game => game.sport === sport).length} games</option>`).join("")}</select></label>
      </div>
      <section class="game-grid">${source.map(game => {
        const top = [...game.rows].sort((a,b) => b.geometry - a.geometry).slice(0,3);
        return `<article class="game-card"><div class="game-head"><div><h3>${esc(game.matchup)}</h3><span>${esc(game.time)}</span></div><span>${game.rows.length} signals</span></div><div class="game-picks">${top.map(row => `<div class="game-pick" data-selection-id="${esc(row.id)}" role="button" tabindex="0"><span class="market-token">${esc(row.market)}</span><div><strong>${esc(row.playerName)}</strong><span>${esc(row.line)} · ${esc(row.verdict.label)}</span></div><b>${row.geometry}</b></div>`).join("") || "<span>No selections</span>"}</div></article>`;
      }).join("")}</section>`;
  }

  function cardAudit(cardRows) {
    if (!cardRows.length) return { score: 0, duplicatePlayers: 0, repeatedGames: 0, priced: 0, label: "Empty" };
    const playerCounts = countBy(cardRows, row => `${row.sport}:${row.playerId || row.playerName}`);
    const gameCounts = countBy(cardRows, row => `${row.sport}:${row.gameId}`);
    const duplicatePlayers = [...playerCounts.values()].reduce((sum, count) => sum + Math.max(0, count - 1), 0);
    const repeatedGames = [...gameCounts.values()].reduce((sum, count) => sum + Math.max(0, count - 1), 0);
    const geometricMean = Math.exp(cardRows.reduce((sum, row) => sum + Math.log(Math.max(.05, row.geometry / 100)), 0) / cardRows.length);
    const independencePenalty = (.88 ** duplicatePlayers) * (.94 ** repeatedGames);
    const score = Math.round(100 * geometricMean * independencePenalty);
    return {
      score,
      duplicatePlayers,
      repeatedGames,
      priced: cardRows.filter(row => row.priceEdge !== null).length,
      label: score >= 72 ? "Balanced" : score >= 58 ? "Fragile" : "Overloaded",
    };
  }

  function countBy(source, keyFn) {
    const map = new Map();
    source.forEach(item => {
      const key = keyFn(item);
      map.set(key, (map.get(key) || 0) + 1);
    });
    return map;
  }

  function renderCard() {
    const cardRows = saved.map(id => rowMap.get(id)).filter(Boolean);
    const audit = cardAudit(cardRows);
    return `${pageHead("Local signal card", "Build less.", "Keep it coherent.", "The card score is a geometric mean, so one weak leg or correlated cluster can pull the whole construction down.", staleInfo().latest)}
      ${freshnessBanner(latestSlateDate())}
      <section class="card-layout">
        <div class="saved-list">${cardRows.length ? cardRows.map(row => `<div class="saved-row"><div data-selection-id="${esc(row.id)}" role="button" tabindex="0"><strong>${esc(row.playerName)} · ${esc(row.line)}</strong><span>${esc(row.sportLabel)} · ${esc(row.matchup)} · Vector ${row.geometry}</span></div>${tierToken(row.tier)}<button class="remove-button" data-action="remove" data-id="${esc(row.id)}">Remove</button></div>`).join("") : `<div class="empty-state"><strong>No signals saved.</strong>Open any selection and add it to this card.</div>`}</div>
        <aside class="card-audit"><div class="audit-score"><div><span class="section-kicker">Card geometry</span><h2>${esc(audit.label)}</h2></div><strong>${audit.score}</strong></div><ul class="audit-list"><li><span>Selections</span><b>${cardRows.length}</b></li><li><span>Repeated players</span><b>${audit.duplicatePlayers}</b></li><li><span>Repeated games</span><b>${audit.repeatedGames}</b></li><li><span>Price verified</span><b>${audit.priced}/${cardRows.length}</b></li></ul></aside>
      </section>`;
  }

  function renderMethod() {
    const example = [...rows].sort((a,b) => b.geometry - a.geometry)[0];
    return `${pageHead("Transparent ranking", "The math", "has a shape.", "The Vector Index measures whether a signal is strong in several independent directions. It does not pretend that model score, probability, and market value are the same thing.", latestSlateDate())}
      <section class="method-grid">
        <article class="method-card"><span class="section-kicker">Five-axis field</span><h2>Distance + balance</h2><p>Each selection becomes a normalized vector inside its own sport and market. We measure the weighted Euclidean distance from an ideal signal, then blend it with a weighted geometric mean.</p><div class="formula">V = 100 × [0.55(1 − d<sub>ideal</sub>) + 0.45G<sub>weighted</sub>]<br>× coverage × conflict penalties</div><p>A weak axis has curvature: it pulls the field inward. Missing inputs reduce coverage instead of being silently treated as excellent or terrible.</p></article>
        <article class="method-card"><span class="section-kicker">Live example</span><h2>${esc(example?.playerName || "Selection")}</h2>${example ? `<div style="display:grid;place-content:center;padding:12px">${radar(example, true)}</div>` : ""}<div class="dimension-list">${DIMENSIONS.map(dimension => `<div class="dimension"><strong>${esc(dimension.label)}</strong><span>${methodCopy(dimension.key)}</span></div>`).join("")}</div></article>
      </section>`;
  }

  function methodCopy(key) {
    return {
      strength: "Percentile rank of the raw model score inside the same sport and market.",
      probability: "Percentile rank of simulated or modeled hit probability among comparable selections.",
      value: "Difference between model probability and the probability implied by available odds.",
      conviction: "Exported confidence, with tier used only when confidence is unavailable.",
      coherence: "Agreement between model-strength rank and probability rank; disagreement bends the shape inward.",
    }[key];
  }

  function routeInfo() {
    const hash = location.hash.replace(/^#/, "") || "today";
    const [route, argument] = hash.split("/");
    return { route, argument };
  }

  function renderNav() {
    const availableSports = Object.keys(SPORT_META).filter(sport => rows.some(row => row.sport === sport));
    const links = [
      { route: "today", href: "#today", label: "Today", core: true },
      ...availableSports.map(sport => ({ route: `sport/${sport}`, href: `#sport/${sport}`, label: SPORT_META[sport].label, count: games.filter(game => game.sport === sport).length })),
      { route: "games", href: "#games", label: "Games", core: true },
      { route: "card", href: "#card", label: "My Card", count: saved.length, core: true },
      { route: "method", href: "#method", label: "Method" },
    ];
    const active = routeInfo();
    const current = active.route === "sport" ? `sport/${active.argument}` : active.route;
    nav.innerHTML = links.map(link => `<a class="nav-link${current === link.route ? " active" : ""}" data-route="${esc(link.route)}" data-core="${link.core ? "true" : "false"}" href="${esc(link.href)}">${esc(link.label)}${link.count !== undefined ? `<span class="nav-count">${link.count}</span>` : ""}</a>`).join("");
    mobileNav.innerHTML = availableSports.map(sport => `<a href="#sport/${sport}" class="${current === `sport/${sport}` ? "active" : ""}">${esc(SPORT_META[sport].label)}</a>`).join("");
  }

  function render() {
    if (!rows.length) return;
    renderNav();
    const route = routeInfo();
    if (route.route === "sport") app.innerHTML = renderSport(route.argument || "mlb");
    else if (route.route === "games") app.innerHTML = renderGames();
    else if (route.route === "card") app.innerHTML = renderCard();
    else if (route.route === "method") app.innerHTML = renderMethod();
    else app.innerHTML = renderToday();
    document.title = `The Board · ${route.route === "sport" ? SPORT_META[route.argument]?.label || "Sport" : route.route[0].toUpperCase() + route.route.slice(1)}`;
    const routeDate = route.route === "sport"
      ? snapshot.sports[route.argument]?.date
      : route.route === "games"
        ? snapshot.sports[gameSport]?.date
        : latestSlateDate();
    const freshness = staleInfo(routeDate);
    headerFreshness.textContent = freshness.stale ? `Historical · ${freshness.latest}` : `Current · ${freshness.latest}`;
    headerFreshness.parentElement.classList.toggle("stale", freshness.stale);
  }

  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => { renderQueued = false; render(); });
  }

  function openDrawer(id) {
    const row = rowMap.get(id);
    if (!row) return;
    const isSaved = saved.includes(id);
    const factorCards = [
      ["Raw model", row.score.toFixed(1), `Tier ${row.tier}`],
      ["Sim probability", probabilityText(row), row.probability === null ? "Not exported" : "market-relative"],
      ["Market implied", row.impliedProbability === null ? "—" : `${(row.impliedProbability * 100).toFixed(1)}%`, priceText(row)],
      ["Price value", row.priceEdge === null ? "—" : `${row.priceEdge >= 0 ? "+" : ""}${(row.priceEdge * 100).toFixed(1)}pp`, row.priceEdge === null ? "unverified" : "model minus implied"],
      ["Projection", row.evidence.projection === null ? "—" : row.evidence.projection.toFixed(1), row.projectionDelta === null ? "not structured" : `${row.projectionDelta >= 0 ? "+" : ""}${row.projectionDelta.toFixed(1)} vs line`],
      ["Coverage", `${Math.round(row.coverage * 100)}%`, `${DIMENSIONS.filter(d => row.vector[d.key] !== null).length}/5 axes`],
    ];
    drawerContent.innerHTML = `<div class="drawer-hero"><div><span class="sport-token" data-sport="${esc(row.sport)}">${esc(row.sportLabel)} · ${esc(row.market)}</span><h2>${esc(row.playerName)}</h2><div class="signal-meta">${esc(row.team)} vs ${esc(row.opponent)} · ${esc(row.time)}</div><div class="drawer-line">${esc(row.line)} · ${esc(priceText(row))}</div></div>${radar(row, true)}</div>
      <section class="drawer-section"><h3>Signal field</h3><div class="factor-grid">${factorCards.map(([label, value, note]) => `<div class="factor-card"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`).join("")}</div>${row.flags.length ? `<div class="stale-banner" style="margin:12px 0 0"><span><strong>Conflict check</strong> · ${esc(row.flags.join(" · "))}</span></div>` : ""}</section>
      <section class="drawer-section"><h3>Evidence surfaced</h3><div class="factor-grid">${evidenceCards(row).join("") || `<div class="factor-card"><span>Structured evidence</span><strong>Limited</strong><small>Exporter should emit factor fields directly.</small></div>`}</div></section>
      <section class="drawer-section"><h3>Full model note</h3><details class="reason-box"><summary>Open original scorer output</summary>${esc(row.reason)}</details></section>
      <button class="drawer-action${isSaved ? " saved" : ""}" data-action="toggle-save" data-id="${esc(id)}">${isSaved ? "Remove from My Card" : "Add to My Card"}</button>`;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    drawerBackdrop.hidden = false;
    document.body.style.overflow = "hidden";
  }

  function evidenceCards(row) {
    const entries = [
      ["L5", row.evidence.l5], ["L10", row.evidence.l10], ["Matchup", row.evidence.matchup],
      ["H2H", row.evidence.h2h], ["Usage", row.evidence.usage],
      ["Minutes", row.evidence.minutes === null ? null : row.evidence.minutes.toFixed(1)],
      ["Lineup", row.evidence.lineup],
    ].filter(([, value]) => value !== null && value !== undefined && value !== "");
    return entries.map(([label, value]) => `<div class="factor-card"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`);
  }

  function closeDrawer() {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    drawerBackdrop.hidden = true;
    document.body.style.overflow = "";
  }

  function notify(message) {
    toast.textContent = message;
    toast.classList.add("show");
    setTimeout(() => toast.classList.remove("show"), 1800);
  }

  function toggleSave(id) {
    if (saved.includes(id)) {
      saved = saved.filter(item => item !== id);
      notify("Removed from My Card");
    } else {
      saved.push(id);
      notify("Added to My Card");
    }
    writeSaved();
    renderNav();
    if (drawer.classList.contains("open")) openDrawer(id);
    if (routeInfo().route === "card") render();
  }

  async function loadSnapshot() {
    if (window.BOARD_SNAPSHOT?.sports) return window.BOARD_SNAPSHOT;
    const sports = {};
    await Promise.all(Object.keys(SPORT_META).map(async sport => {
      try {
        const response = await fetch(`data/${sport}.json`, { cache: "no-store" });
        if (response.ok) sports[sport] = await response.json();
      } catch { /* file:// uses snapshot.js */ }
    }));
    return { generated_at: new Date().toISOString(), sports };
  }

  async function init() {
    snapshot = await loadSnapshot();
    const flattened = flattenSnapshot(snapshot);
    rows = flattened.rows;
    games = flattened.games;
    applyGeometry(rows);
    rowMap = new Map(rows.map(row => [row.id, row]));
    saved = saved.filter(id => rowMap.has(id));
    writeSaved();
    if (!rows.length) {
      app.innerHTML = `<div class="empty-state"><strong>The data bundle is empty.</strong>Run the board pipeline or regenerate data/snapshot.js.</div>`;
      return;
    }
    render();
  }

  document.addEventListener("click", event => {
    const selection = event.target.closest("[data-selection-id]");
    if (selection) { openDrawer(selection.dataset.selectionId); return; }
    const action = event.target.closest("[data-action]");
    if (!action) return;
    if (action.dataset.action === "more") {
      limits[action.dataset.sport] = (limits[action.dataset.sport] || 25) + 25;
      render();
    } else if (action.dataset.action === "toggle-save") toggleSave(action.dataset.id);
    else if (action.dataset.action === "remove") { toggleSave(action.dataset.id); render(); }
  });

  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeDrawer();
    if ((event.key === "Enter" || event.key === " ") && event.target.matches("[data-selection-id]")) {
      event.preventDefault();
      openDrawer(event.target.dataset.selectionId);
    }
  });

  document.addEventListener("input", event => {
    const control = event.target.closest("[data-filter]");
    if (!control || !control.dataset.sport || control.dataset.filter !== "search") return;
    sportFilters(control.dataset.sport)[control.dataset.filter] = control.value;
    clearTimeout(searchTimer);
    const sport = control.dataset.sport;
    searchTimer = setTimeout(() => {
      render();
      const replacement = document.querySelector(`[data-filter="search"][data-sport="${CSS.escape(sport)}"]`);
      if (replacement) {
        replacement.focus();
        replacement.setSelectionRange(replacement.value.length, replacement.value.length);
      }
    }, 140);
  });

  document.addEventListener("change", event => {
    const control = event.target.closest("[data-filter]");
    if (!control) return;
    if (control.dataset.filter === "game-sport") { gameSport = control.value; render(); return; }
    if (control.dataset.sport) {
      sportFilters(control.dataset.sport)[control.dataset.filter] = control.value;
      render();
    }
  });

  window.addEventListener("hashchange", () => { closeDrawer(); render(); window.scrollTo(0, 0); });
  drawerClose.addEventListener("click", closeDrawer);
  drawerBackdrop.addEventListener("click", closeDrawer);

  window.__BOARD_TEST__ = {
    americanToProbability,
    applyGeometry,
    cardAudit,
    parseEvidence,
    thresholdFromLine,
    getRows: () => rows,
  };

  init().catch(error => {
    console.error(error);
    app.innerHTML = `<div class="empty-state"><strong>The signal desk could not initialize.</strong>${esc(error.message)}</div>`;
  });
})();
