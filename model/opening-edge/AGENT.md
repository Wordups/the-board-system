---
name: opening-edge-universal
description: Run the Opening Edge opening-sequence research system for first-basket or first-score markets. Use when Codex needs to ingest a daily sports slate, extract tip or opening-possession events, distinguish first points from first field goal and first team basket, audit player/team counts, calculate transparent rankings, compare sportsbook prices, generate straights or 2/3/4-leg and round-robin research cards, refresh the Opening Edge dashboard, or produce Discord-ready reports. WNBA is the implemented adapter; require a validated play-by-play adapter before applying the model to another league.
---

# Opening Edge Universal

Operate an auditable research pipeline. Never substitute sportsbook ordering, an AI overview, or a recent win streak for event data.

## Run order

1. Confirm the league, slate date, matchups, and exact market:
   - `first_field_goal`: first made field goal by either team; exclude free throws.
   - `first_points`: first points by either team; include free throws.
   - `first_team_basket`: one named team's first made field goal.
2. Browse current schedules, official availability, starters, trades, and odds. Prefer official league gamebooks and play-by-play; use the ESPN adapter as the machine-readable feed and reconcile discrepancies.
3. For WNBA model work, locate the Opening Edge project. Default to the known project path in `references/implementation.md`; if absent, search with `rg --files -g package.json` and identify the project containing `lib/wnba/model.ts`.
4. Refresh data with the project's `npm run model:sync -- START END` command. Use the current season start and the day before the slate unless the user requests a different window.
5. Run `npm run test:model` and `npm run build`. Do not publish rankings from a failed refresh, failed audit, or failed test.
6. Run `scripts/audit_snapshot.py PATH_TO/data/wnba-model.json`. Resolve every error. Disclose warnings caused by missing or small samples.
7. Read the compact `data/wnba-board.json`. Apply current matchup, starter, health, role and price inputs; do not silently treat their neutral defaults as verified facts.
8. Produce only the report sections requested. For daily cards, show decisive raw counts and denominators before selections.

## Decision rules

- Separate tip winner, possession recipient, first shooting attempt, first made field goal, and first points.
- Treat a player's team-first attempt rate and conversion as different features from whole-game first-basket frequency.
- Weight the opening tip meaningfully, but never infer that the tip winner must score first.
- Preserve denominators. Write `5/31`, not only `5` or `16.1%`.
- Flag recent returns, trades, lineup changes, restrictions and stale team labels.
- Treat cross-game legs as a shared profile, not statistical correlation. Opposing legs in one game are mutually exclusive.
- Keep stake discussion bounded. Never recommend chasing, debt, paycheck, bill or essential money.
- Do not automate sportsbook login, wager placement or cash-out.

## Scoring

Use the rubric in `references/model-spec.md`. The computed score is a ranking aid, not calibrated probability. If odds are present, show market-implied probability separately and label any apparent edge as provisional until lineup/price inputs are verified.

## Outputs

- **Team audit:** full player counts whose sum equals the team's whole-game first-basket total.
- **Matchup report:** tip rates, first-attempt ownership, first-attempt makes, first-basket rates, availability, and price.
- **Card:** primary, backup and optional shared-profile combinations; label every first-basket parlay high variance.
- **Dashboard:** refresh `wnba-board.json`, verify `/api/board`, test, build, then deploy only when requested or when continuing an existing Sites build workflow.
- **Discord:** use the compact template in `references/report-template.md`.

## New league adapters

Do not generalize WNBA parsing by text substitution. Before enabling another league, implement and test a league adapter that maps its feed into the canonical event schema in `references/event-schema.md`, including a known-game fixture with tip/kickoff/faceoff, first attempt, miss branch and first score.

## Portable export

When the user asks to use this agent in Claude, another model, or an external orchestration setup, run `scripts/export_agent.py PROJECT OUTPUT_DIR`. Deliver the generated folder and ZIP. Treat `AGENT.md` as the canonical vendor-neutral instructions; platform-specific entrypoints must point back to it instead of duplicating policy.
