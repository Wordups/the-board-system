# The Board — a self-running sports probability engine

A multi-sport prediction system that ingests live data, fits **calibrated
probability models**, scores every available play through a geometric *Vector
Index*, and deploys a static dashboard — refreshing itself **hourly via CI with
zero human in the loop**. Built and shipped solo.

📖 **[Read the full build story →](STORY.md)** — the problem, the modeling, and
the hard parts (calibration, the World Cup roster fallback, encoding model-vs-market
disagreement as geometric distance).

> Odds here are **model-derived**, not from a real sportsbook. The goal was a
> pipeline that turns messy public data into honest, calibrated probabilities
> and serves them unattended — not to beat a book.

## How it works

```
ESPN public APIs ──▶ collectors ──▶ probability models ──▶ Vector Index ──▶ static dashboard
   (MLB/NBA/NFL/      (normalize     (Poisson geometry,      (5-axis score,    (Today / per-sport /
    WNBA/soccer/       feeds)         shrink-to-prior,        price + data       Games / My Card)
    tennis)                           de-vigged calibration)  penalties)
        ▲                                                                              │
        └──────────────── GitHub Actions: hourly refresh ▸ commit ▸ auto-deploy ◀──────┘
```

- **Calibrated, not just collected** — match probabilities are fit against
  de-vigged market odds; counting stats are shrunk toward position priors so a
  one-game sample can't masquerade as a trend.
- **The Vector Index** — each candidate is scored by its geometric distance from
  an ideal signal, with explicit penalties for price conflict, projection
  conflict, missing data, and correlation. A high raw score that fights its own
  line gets pulled *down*, not surfaced.
- **Self-refreshing** — a GitHub Actions loop pulls fresh data hourly,
  regenerates exports + the browser bundle, commits to `main`, and auto-deploys
  the static site. A loop-guard stops its own data commits from re-triggering it.

## Stack

Python (collectors · models · calibration · Poisson math) · single-file vanilla
JS dashboard (no framework, deploys static, runs over `file://`) · GitHub Actions
(self-refresh loop) · GitHub Pages (delivery) · 60+ backend tests gating the
pipeline.

---

<details>
<summary><b>Repo internals — data board layers</b></summary>

### MLB research / parlay layer

The MLB export includes a `research_board` section inside `mlb.json`, designed to
sit on top of the core model, not replace it:

- `home_run` with `hr_of_day` and 2 / 3 / 4 / 6 leg parlays
- separate `hits`, `total_bases`, and `strikeouts` boards
- optional outside-research overlay via `backend/data_raw/mlb_research_notes.json`

Use `backend/data_raw/mlb_research_notes.example.json` as the template for manual
source notes (X, TeamRankings, beat writers, lineup notes, weather).

### NBA research / stack layer

The NBA export includes a `research_board` section inside `nba.json`, organized for:

- `top_strip` for best stacked plays across the top
- `safe_plays` for higher-floor combinations
- `long_shots` for higher-variance money plays
- sectioned boards for `PTS`, `AST`, `REB`, and `3PM`

Use `backend/data_raw/nba_research_notes.example.json` as the template for outside
notes (minutes caps, rotation changes, matchup notes, manual long-shot tags).

### NFL: two categories on one export

`nfl.json` carries a `categories` block naming both, and each has its own
section of the payload.

**1. Pure predictions.** Markets are `PASS_YDS` / `RUSH_YDS` / `REC_YDS` /
`REC` / `TD` / `ML`, quoted on the grid books actually post (25-yard steps for
passing, 10 for rushing and receiving). Line selection follows the same
value-pricing rule as NBA/WNBA — ship the rung whose shrunken hit rate lands
closest to 0.50, not the one engineered to look safe.

The simulator models football's real state axis, **game script**, rather than
basketball's minutes risk: an underdog throws more and runs less, a favorite
does the opposite, and a blowout takes both away. Passing and rushing read
opposite multiplier tables off the same sampled state
(`NFL_PASS_*` / `NFL_RUSH_*` in `backend/app/sim/outcome_models.py`), driven by
the game's spread, total, and whether it's indoors. Every prop quotes a full
ladder — alternate yardage rungs via the latent-normal shift, and 1+/2+ TD via
a Poisson rate backed out of the anytime probability.

**2. Salary categories (historical).** Contracted players are bucketed into
league-wide money bands by APY (SUPERMAX $40M+ → ROOKIE_MIN under $2.5M), then
reduced to per-game PPR points across a **three-season rolling window**. Each
tier reports its distribution (median, quartiles, points per $1M), and the
slate is placed against those baselines — `value_index` of 1.00 means a player
is returning exactly what his pay grade historically returns.

This direction is one-way on purpose: the salary board reads the prediction
candidates to know who is playing, but no money signal feeds back into a
prediction score. Contract value describes a player; it is not evidence about
Sunday.

**Contract data.** There is no free live feed, so money comes from a local
file — `backend/data_static/nfl_contracts.json` (tracked, CI sees it), which a
private `backend/data_raw/nfl_contracts.json` overrides if present. The
committed file is a **hand-entered seed**: every row is marked
`estimated: true` and every figure is approximate. Replace it before treating
any tier number as fact:

```bash
python backend/scripts/import_nfl_contracts.py spotrac-export.csv \
  --source "Spotrac 2026-08-14" --as-of 2026-08
```

A missing or malformed contracts file is not a build failure — the salary
category reports `available: false` with a reason and the prediction board
ships as normal.

</details>
