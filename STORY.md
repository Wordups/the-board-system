# The Board — building a self-running sports probability engine

**TL;DR** — A multi-sport prediction system that ingests live data, fits
calibrated probability models, scores every available play through a geometric
"Vector Index," and deploys a static dashboard — refreshing itself hourly with
zero human in the loop. Built and shipped solo.

> The odds in this project are **model-derived**, not sourced from a real
> sportsbook. The point was never to beat a book — it was to build a pipeline
> that turns messy public data into calibrated, defensible probabilities and
> serves them without anyone babysitting it.

---

## The problem

Sports "pick" sites all have the same two failures:

1. **They show a score, not a probability.** A player ranked "89/100" tells you
   nothing about whether that's a good *bet* — an 89 can sit on top of a line
   the model itself thinks is a coin flip.
2. **They drown you.** MLB alone is ~15 games and 260+ selections a day. Stack
   five boards on top of each other and the signal disappears.

I wanted the opposite: a system that answers *"what should I actually consider,
and why,"* and that keeps answering it every hour without me touching it.

## What I built

A pipeline with four honest stages:

- **Ingestion** — collectors pull live data from public ESPN endpoints across
  MLB, WNBA, soccer (FIFA World Cup), and tennis, normalizing wildly different
  feeds into one candidate shape.
- **Modeling** — per-market probability models. Goals and first-half outcomes
  use Poisson geometry; counting stats are shrunk toward position priors so a
  one-game World Cup sample doesn't masquerade as a trend. Match probabilities
  are calibrated against **de-vigged** market odds before any geometry runs.
- **Scoring — the Vector Index** — every candidate is placed in a five-axis
  space and scored by its *geometric distance from an ideal signal*, with
  explicit penalties for price conflict, projection conflict, missing data, and
  correlation. This is the core idea: a high raw score that conflicts with its
  own line gets pulled *down*, not surfaced as a top play.
- **Delivery** — a static, decision-first dashboard (Today, per-sport boards,
  Games, My Card, Method) that runs even over `file://` via a pre-built data
  bundle. No server required.

## The hard part

**Calibration, not collection.** Pulling data is easy. The real work was making
the probabilities *honest*:

- National-team rosters during the World Cup expose an all-zeros in-tournament
  stat split. Naively, every player models as a non-scorer. The fix was a
  fallback that pulls each player's aggregated club + international overview and
  rebuilds a real scoring profile from recent seasons — then shrinks it by
  sample size so one match can't dominate.
- A raw model score and a market line disagree constantly. Instead of hiding
  that, the Vector Index *encodes the disagreement as distance* — so the UI can
  say "highly ranked, but the price says coin flip" instead of pretending the
  conflict isn't there.
- Repetition. The same player would surface in the hero, the straight pick, and
  three parlays. The rebuild made selections canonical and had every board
  reference them, so nothing repeats.

## How it runs itself

A GitHub Actions workflow fires hourly (and at the daily freeze gate), pulls
fresh data, regenerates every export and the browser bundle, commits the result
back to `main`, and the static site auto-deploys. A loop-guard keeps the
workflow's own data commits from re-triggering it. It has been refreshing
unattended for months.

## Stack

Python (collectors, models, calibration, Poisson math) · single-file vanilla JS
dashboard (no framework, deploys as static) · GitHub Actions for the
self-refresh loop · GitHub Pages for delivery. 60+ backend tests gate the
pipeline.

## What I'd build next

Pull a real EV signal by comparing the model's probabilities against live
Polymarket / FanDuel / DraftKings prices, and surface a single "+EV straight
bet of the day" — the one play where the model and the market disagree enough to
matter.
