# Opening Edge model (WNBA first-basket)

Ported from the Codex `opening-edge` project; governed by the
`opening-edge-universal` skill. Zero-dependency Node (>=22) + Python for audit.

Pipeline (run from this directory, in order):

```bash
# 1. Sync season play-by-play from ESPN (start → day before slate)
node --experimental-strip-types scripts/sync-wnba.ts 2026-05-01 2026-08-04

# 2. Tests + audit — do not publish from a failed refresh
node --experimental-strip-types --test tests/wnba-model.test.ts
python scripts/audit_snapshot.py data/wnba-model.json

# 3. Emit the #opening section data (re-scores with today's real matchups)
node --experimental-strip-types scripts/generate-section.ts \
  --date 2026-08-05 --label "Wednesday, Aug 5" \
  --slate "PHX@ATL=7:00 PM ET,SEA@NY=7:00 PM ET,DAL@WSH=7:30 PM ET,LA@CHI=9:00 PM ET"
```

Step 3 writes `data/opening-edge.js` at the repo root — the only file the page
reads. Slate uses ESPN abbreviations (WSH, NY, GS, LV); display abbreviations
are mapped in the generator.

Rules carried over from the skill: preserve denominators (write `5/30`, never
just `5`), prices are model-fair lines (no sportsbook feed) and the edge score
is a ranking aid, not calibrated probability. First-basket markets are high
variance; never automate wager placement.
