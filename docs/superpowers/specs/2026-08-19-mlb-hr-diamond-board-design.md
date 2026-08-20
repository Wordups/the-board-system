# MLB HR Board + Diamond of the Day — Design

## Problem

The live MLB tab (`assets/board.js`, the "Signal Desk" rewrite) renders every
market — Hits, HR, TB, K, RBI — as one large `signalCard` per row in a single
mixed list, ranked by a "Vector" score that structurally favors easy Hits
props (70-90% hit rate) over HR props (~40% ceiling). HR picks get buried, and
each card is a full-width radar-chart tile — one name at a time, not a
scannable board.

The backend still computes exactly the data needed to fix this — `diamond`
(a 5-position "Diamond of the Day": 1B/2B/3B/HOME/MOUND, each a hand-picked
best play by a different heuristic) and per-player HR scores/tiers inside the
flattened row set — but the frontend rewrite never wired either back in. This
is a frontend-only fix: no backend, scoring, or model changes.

## Goals

- Bring back a scannable, HR-focused view on the MLB tab: today's single best
  HR threat per team (graded A/B/C), plus the literal 5-position Diamond of
  the Day.
- Fix the "meh" visual specifically: replace the one-giant-card-per-row
  pattern with a dense grid of compact cards for these two boards.
- Leave everything the user already likes untouched: the "Best balanced
  signals" cards, the market/tier/sort filter toolbar, and the full ranked
  table below.

## Non-goals

- No backend/scoring changes. `data.diamond` and per-player HR scores are
  already correct; this is presentation only.
- No changes to non-MLB sports.
- No changes to the existing flat table, filters, or "Best balanced signals"
  section.

## Data sources

Two different sources, because they answer different questions:

1. **Diamond of the Day** — reads `snapshot.sports.mlb.diamond.picks` (an
   object keyed `1B`/`2B`/`3B`/`HOME`/`MOUND`) directly from the raw snapshot.
   This is pre-computed by the backend with specific selection heuristics per
   slot (e.g. 1B = "highest-prob Hit/TB base", MOUND = "best pitcher K") —
   that logic is not reproducible from the generic flattened rows, so it must
   be read as-is.
2. **Best HR Per Team** — computed client-side from the already-flattened
   `rows` array (built by the existing `flattenSnapshot`): filter to
   `sport === "mlb" && market === "HR"`, group by `team`, keep the
   highest-`score` row per team, sort the resulting list by `score` desc.
   New logic, ~15 lines, no new data plumbing.

## Placement

In `renderSport("mlb")`, insert a new block between `gamesRail(sportGames)`
and the existing "Current profile / Best balanced signals" section head.
Nothing after that point in the function changes.

## Components

### Compact card (new)

A small grid tile, replacing `signalCard`'s radar-chart layout for these two
boards only (`signalCard` itself is untouched and keeps serving "Best
balanced signals" and the drawer). Shows: rank or position label, team
abbreviation, player name, opponent, line, score, and the existing
`tierToken()` A/B/C badge (reused as-is — no new grading logic or styling).
Laid out via CSS grid, 3–5 per row on desktop, matching the density of the
old `core-grid` pattern from git history (commit `6048179e`) rather than
today's one-per-row cards.

### Diamond of the Day section

Header ("Diamond of the Day" + today's date), then 5 compact cards in
position order (1B, 2B, 3B, HOME, MOUND), each labeled with its position
instead of a rank number.

### Best HR Per Team section

Header ("Best HR Per Team"), then compact cards for every team with a game
today, ordered by score descending. Team count varies with the day's slate
(no fixed number).

## Interactivity

Both sections' cards use the existing `data-selection-id` + `openDrawer(id)`
pattern. For Best HR Per Team this resolves directly since the cards are
built from real `rows` entries. For Diamond of the Day, a pick's `player_id`
is matched against `rows` to find its id; if no match is found (edge case —
diamond pick references a market/line not present in the flattened rows),
the card renders without click interactivity rather than erroring.

## Testing

Manual verification only (per project convention — this repo has no frontend
test harness): load the MLB tab locally against current `data/mlb.json`,
confirm both new sections render with real names/grades, confirm existing
sections are visually unchanged, confirm card clicks open the drawer where
data resolves and degrade gracefully where it doesn't.
