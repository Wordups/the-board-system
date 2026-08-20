# NFL Correlation Board — Design

## Problem

The-board-system has a disabled "NFL — coming soon" nav tab and three
placeholder files (`backend/app/collectors/nfl_collector.py`,
`backend/app/models/nfl_model.py`, `data_final/nfl.json` — all one line /
empty). Nothing NFL-specific exists. The user wants a real NFL player-props
+ same-game-correlation board live in time for the 2026 Week 1 slate,
modeled after the RotoWire "Break the Plane" idiom (a predictive grade per
player, stacked against a price, ranked) and built the same way every other
sport in this repo was: collector → model → scoring/tiers → builder →
`data/nfl.json`, reusing the generic frontend that already renders any sport
in that shape.

## Goals

- Markets: **Anytime TD** (rush + rec), **Receiving Yards**, **Rushing
  Yards**, **Receptions**, **Passing TDs**, **Money Line** (model-fair team
  win probability — this repo has no real sportsbook feed for any sport, ML
  follows the same convention).
- A **same-game correlation board**: pairs of plays that tend to co-occur in
  the same game (e.g. a team's QB passing-TD line + their top target's TD),
  with a joint probability — the actual "correlation" the user asked for,
  not just independent per-player grades.
- Historical-stats- and trend-driven, since Week 1 has zero current-season
  sample to lean on.
- Ships inside the existing repo/pipeline, activates the existing (currently
  disabled) NFL nav tab.

## Non-goals

- No real sportsbook odds ingestion (matches every other sport here).
- No player-prop markets beyond the six listed (kicking, defense/special
  teams props, etc. are out of scope for this spec).
- No changes to MLB/WNBA/soccer/tennis pipelines or frontend code — this is
  additive only, reusing shared modules where they already generalize.

## Data collection

New `backend/app/collectors/nfl_collector.py`, following the existing
`soccer_collector.py` pattern (ESPN public API, `requests`, `ThreadPoolExecutor`
for parallel roster/team fetches, written through `app/outputs/json_writer.py`):

- **Schedule** — Week 1 2026 matchups via ESPN's NFL scoreboard endpoint.
- **Rosters + depth charts** — current rosters, flagged as pre-final in
  August; carries a `lineup_confirmed`/uncertain flag through to output,
  same mechanism the WNBA pipeline already uses for DTD/roster-sync
  (`sync-rosters.ts` equivalent, ported to Python for this pipeline).
- **Historical stats** — prior-season (2025) per-player splits for every
  target market (TDs, yards, receptions, pass TDs), since current-season
  Week 1 has nothing to sample yet. This is the primary signal source, per
  the user's explicit direction to lean on "rosters, historical stats, and
  trends leading up to the games."
- **Team defense-by-position** — opponent's prior-season rate allowed to
  each position group (e.g. receiving yards/TDs allowed to WRs vs RBs vs
  TEs) — this is the matchup-concept signal driving the correlation logic.

## Model approach

Reuses existing scoring primitives rather than inventing new math:

- **Per-market player probability** (`backend/app/models/nfl_model.py`):
  historical per-game rate for the market, shrunk toward a position prior
  (same shrink-to-prior approach as `app/scoring/prob_shrinkage.py`), then
  adjusted by the opponent's positional defense rate. Output as a simulated
  probability (0-1), same convention as `sim_prob_pct` elsewhere.
- **Money line**: a power-rating model (points-differential based) →
  win probability → `hit_rate_to_implied_odds()` (`app/scoring/value.py:36`)
  for the model-fair price, identical mechanism to every other sport's ML.
- **Same-game correlation**: a new per-game Monte Carlo drive simulation in
  `backend/app/sim/` (own module, does not touch `sim_engine.py`'s
  MLB-specific logic) — possession → scoring type → player attribution,
  seeded and vectorized like the existing sim engine. Emits joint
  probabilities for 2-leg same-game pairs (QB pass-TD + pass-catcher's
  TD/yards/receptions), the same shape as the Opening Edge sim-combo
  (`generate-section.ts`'s per-game leaderboard + best cross-game 2-leg
  pattern, adapted to same-game pairs instead of cross-game).
- **Tier cutoffs**: `app/scoring/tiers.py` currently uses one fixed 0-100
  scale (A=35/B=22/C=12) calibrated to MLB HR's ~40% practical ceiling. NFL
  markets have very different ceilings (a bellcow RB's anytime-TD prob can
  clear 60-70%; a receptions line can sit near 50/50) — reusing the global
  cutoffs as-is would misgrade NFL picks relative to MLB's calibration.
  **Open question for the implementation plan**: either derive NFL-specific
  cutoffs per market (mirroring how HR's ~40% ceiling drove MLB's cutoffs),
  or add per-sport cutoff overrides to `tiers.py`. Flagging here rather than
  guessing — this affects every card's grade.

## Output shape / frontend integration

`backend/app/builders/` gets a new `nfl_board_builder.py` using
`empty_markets_for(["TD", "RecYds", "RushYds", "REC", "PassTD", "ML"])`
(`app/builders/universal_game_builder.py:15`) so `data/nfl.json` matches the
same `games[].markets` shape MLB/WNBA/soccer already use, validated through
the existing `board_schema.BoardPayload` (`app/schemas/board_schema.py`).

Because `assets/board.js`'s `flattenSnapshot()` reads any sport generically
from `data.sports[sport].games[].markets`, **the base board (filters, tier
badges, signal cards, drawer, ranked table) needs zero new frontend code** —
it activates the moment `nfl.json` is real and the nav tab is un-disabled.

The **same-game correlation board** is the one genuinely new frontend piece:
a dedicated section on the NFL tab (own render function in `board.js`,
following the visual pattern already used for WNBA's sim-combo cards),
reading a new `same_game_pairs` field the builder adds to `nfl.json` outside
the generic `markets` shape (same "extra field the generic pipeline ignores"
pattern `data.diamond` already uses for MLB).

## Constraints / honesty

Week 1 carries materially more uncertainty than a mid-season slate — no
current-season sample, depth charts still settling in August. The board
surfaces this directly (a "Week 1 — limited current-season signal" banner,
same spirit as the existing stale-data banner), rather than presenting
Week 1 grades with the same implied confidence as a July MLB slate.

## Testing

Follows repo convention: pytest coverage for the collector (mocked ESPN
responses), model (shrinkage/matchup-adjustment math on fixed inputs), and
builder (schema validation against `board_schema`), plus a manual pass
loading the NFL tab locally against a real Week 1 `nfl.json` to confirm the
generic board renders correctly and the correlation section shows sane
joint probabilities before this goes live.
