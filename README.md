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
   (MLB/WNBA/         (normalize     (Poisson geometry,      (5-axis score,    (Today / per-sport /
    soccer/tennis)     feeds)         shrink-to-prior,        price + data       Games / My Card)
                                      de-vigged calibration)  penalties)
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

</details>
