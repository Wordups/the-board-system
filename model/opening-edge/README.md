# Opening Edge Universal — vendored agent (v1.0.1)

This is the portable `opening-edge-universal-agent` package, vendored verbatim.
`AGENT.md` is the canonical instruction set — follow its run order; `CLAUDE.md`
/ `SYSTEM_PROMPT.md` are platform entrypoints and `agent.json` the manifest.
Zero-dependency Node (>=22) + Python for the audit.

Local modifications to the vendored package (everything else is verbatim):

- `lib/leagues.ts`, `lib/sync.ts` — new. League config (ESPN endpoint, team
  set, display abbreviations, headshot base, season window, output files)
  and the shared season-sync body.
- `lib/wnba/espn.ts`, `lib/wnba/model.ts` — take an optional league argument
  that **defaults to WNBA**, so every pre-existing call site is unchanged.
  The basketball core itself is league-agnostic and was not otherwise
  touched.
- `scripts/generate-section.ts` — the board-integration script (local from
  the start); now `--league`-aware and emits the `first_team_basket` market.
- `scripts/sync-nba.ts`, `tests/nba-model.test.ts` — new NBA entrypoint and
  the adapter fixture AGENT.md requires before enabling a league.

Board integration:

```bash
# 1. Sync season play-by-play from ESPN (start → day before slate)
node --experimental-strip-types scripts/sync-wnba.ts 2026-05-01 2026-08-04
node --experimental-strip-types scripts/sync-nba.ts   2026-10-01 2027-01-28

# 2. Validate — do not publish rankings from a failed refresh
node --experimental-strip-types --test tests/wnba-model.test.ts tests/nba-model.test.ts
python scripts/audit_snapshot.py data/wnba-model.json
python scripts/audit_snapshot.py data/nba-model.json

# 2b. Optional: refresh the triple-double watch (ESPN season averages);
#     wins ledger lives in data/wins.json (hand-maintained)
node --experimental-strip-types scripts/sync-td-watch.ts 2026
node --experimental-strip-types scripts/sync-td-watch.ts 2026 nba

# 3. Emit the #opening section (re-scores with today's real matchups;
#    pass the ESPN injury report so lineup context is flagged)
node --experimental-strip-types scripts/generate-section.ts \
  --date 2026-08-05 --label "Wednesday, Aug 5" \
  --slate "PHX@ATL=7:00 PM ET,SEA@NY=7:00 PM ET,DAL@WSH=7:30 PM ET,LA@CHI=9:00 PM ET" \
  --injuries "ATL:Te-Hina Paopao Out;CHI:Sydney Taylor Day-To-Day,Azura Stevens Out,Skylar Diggins Out,Rickea Jackson Out;NY:Satou Sabally Out,Leonie Fiebich Out;SEA:Taina Mair Out"

node --experimental-strip-types scripts/generate-section.ts --league nba \
  --date 2027-01-29 --label "Thursday, Jan 29" \
  --slate "BKN@DEN=9:00 PM ET,DET@PHX=10:00 PM ET,OKC@MIN=8:00 PM ET"

# Or let the workflow driver do all of it for both leagues (a league with
# no games today is a clean no-op that leaves its board alone):
node --experimental-strip-types scripts/refresh-opening-edge.ts
```

Step 3 writes `data/opening-edge.js` (WNBA, `window.OPENING_EDGE`) or
`data/opening-edge-nba.js` (NBA, `window.OPENING_EDGE_NBA`) at the repo root —
the only files the page reads. Slate/injuries use ESPN abbreviations (WSH, NY,
GS, LV for the WNBA; GS, NO, SA, UTAH for the NBA); display abbreviations are
mapped per league in `lib/leagues.ts`.

Two markets ship on every board, kept separate per AGENT.md: `first_field_goal`
(the game's first made field goal, either team) and `first_team_basket` (one
named team's first made field goal). The team-first board reconciles — each
team's listed counts sum to its games played, because every game produces
exactly one team-first basket.

Carried over from AGENT.md: preserve denominators (`5/30`, never just `5`);
unverified components (role/availability, H2H, market) stay at neutral 0.5 and
are disclosed, never presented as facts; prices are model-fair lines and the
edge score is a ranking aid, not calibrated probability; first-basket markets
are high variance; never automate wager placement.
