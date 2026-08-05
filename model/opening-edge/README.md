# Opening Edge Universal — vendored agent (v1.0.1)

This is the portable `opening-edge-universal-agent` package, vendored verbatim.
`AGENT.md` is the canonical instruction set — follow its run order; `CLAUDE.md`
/ `SYSTEM_PROMPT.md` are platform entrypoints and `agent.json` the manifest.
Zero-dependency Node (>=22) + Python for the audit.

Board integration (the only local addition is `scripts/generate-section.ts`):

```bash
# 1. Sync season play-by-play from ESPN (start → day before slate)
node --experimental-strip-types scripts/sync-wnba.ts 2026-05-01 2026-08-04

# 2. Validate — do not publish rankings from a failed refresh
node --experimental-strip-types --test tests/wnba-model.test.ts
python scripts/audit_snapshot.py data/wnba-model.json

# 2b. Optional: refresh the triple-double watch (ESPN season averages);
#     wins ledger lives in data/wins.json (hand-maintained)
node --experimental-strip-types scripts/sync-td-watch.ts 2026

# 3. Emit the #opening section (re-scores with today's real matchups;
#    pass the ESPN injury report so lineup context is flagged)
node --experimental-strip-types scripts/generate-section.ts \
  --date 2026-08-05 --label "Wednesday, Aug 5" \
  --slate "PHX@ATL=7:00 PM ET,SEA@NY=7:00 PM ET,DAL@WSH=7:30 PM ET,LA@CHI=9:00 PM ET" \
  --injuries "ATL:Te-Hina Paopao Out;CHI:Sydney Taylor Day-To-Day,Azura Stevens Out,Skylar Diggins Out,Rickea Jackson Out;NY:Satou Sabally Out,Leonie Fiebich Out;SEA:Taina Mair Out"
```

Step 3 writes `data/opening-edge.js` at the repo root — the only file the page
reads. Slate/injuries use ESPN abbreviations (WSH, NY, GS, LV); display
abbreviations are mapped in the generator.

Carried over from AGENT.md: preserve denominators (`5/30`, never just `5`);
unverified components (role/availability, H2H, market) stay at neutral 0.5 and
are disclosed, never presented as facts; prices are model-fair lines and the
edge score is a ranking aid, not calibrated probability; first-basket markets
are high variance; never automate wager placement.
