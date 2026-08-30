# Empirical Calibration, Market Weighting, and EV Core

**Date:** 2026-08-29 (amended 2026-08-30)
**Status:** Approved for planning
**Scope:** Spec 1 of 3 (see [Related specs](#related-specs))

## Problem

The board's published probabilities are systematically inflated, and the one
component that already issues betting instructions runs on the inflated number.

This was measured, not assumed. 4,521 board entries across 112 pre-slate
snapshots (2026-04-29 → 2026-08-27) were reconstructed from git history and
graded against actual MLB game logs:

| Board says | n | Actual | Ratio |
|---|---|---|---|
| 20–25% | 911 | 15.8% | 0.67x |
| 25–30% | 1,529 | 17.7% | 0.65x |
| 30–35% | 703 | 17.5% | 0.55x |
| 35%+ | 374 | 19.8% | 0.52x |

Two distinct defects:

1. **Miscalibration.** Every bin above 20% comes in at 0.52–0.67x. The board
   prints 40% on plays that hit at 20%.
2. **No discrimination.** `sim_prob_pct` scores **AUC 0.515** against outcomes
   (0.50 = coin flip). Within-day score rank reaches only 0.537. The board's
   top decile hits 20.8%; its bottom decile hits 13.5%. It separates top from
   bottom weakly and cannot rank finely at all.

`app/scoring/calibration_guardrail.py` already anticipates defect 1 — its
docstring states *"The Monte Carlo is a good SCREEN but a broken PRICER."* But
it compares the sim to a **closed-form theoretical** baseline. Nothing compares
it to **what actually happened**. Only the empirical comparison surfaced the
20.8% ceiling.

### Why this is urgent rather than cosmetic

`app/builders/kalshi_edge.py` computes `edge_pp = model_prob - implied_prob`
using `sim_prob_pct`, then emits BET/PASS/CHECK. With `model_prob` inflated
~1.5–2x, the edge is manufactured out of the inflation itself. Against the live
2026-08-29 ladder board:

| Player | Raw sim | Calibrated | Kalshi implied | Edge raw | Edge calibrated | Now → Correct |
|---|---|---|---|---|---|---|
| Zach Neto | 28.2% | 20.5% | 11.5% | +16.7 | +9.0 | BET → BET |
| Jake Bauers | 28.4% | 20.5% | 17.5% | +10.9 | +3.0 | **BET → PASS** |
| Kyle Schwarber | 38.3% | 20.5% | 30.5% | +7.8 | **−10.0** | **BET → PASS** |
| C. Encarnacion-Strand | 27.5% | 20.4% | 21.5% | +6.0 | −1.1 | **BET → PASS** |
| Gunnar Henderson | 24.3% | 19.2% | 18.5% | +5.8 | +0.7 | **BET → PASS** |

**14 BETs become 7 BETs and 7 PASSes.** Schwarber inverts from a recommended
bet to 10 points of negative edge.

### Live validation, 2026-08-29

Six HR selections were priced and graded against the settled slate.

| Play | Price | Result | P/L on $10 |
|---|---|---|---|
| Zach Neto | 11.5c Kalshi | **HR** | +$76.96 |
| Colton Cowser | +540 book | **HR** | +$54.00 |
| Bryce Eldridge | 11.5c Kalshi | 4-for-9, 0 HR | -$10 |
| Rafael Devers | 14.5c Kalshi | 4-for-9, 0 HR | -$10 |
| Brandon Marsh | +630 book | 1-for-4 | -$10 |
| Corbin Carroll | 14.5c Kalshi | 0-for-9 | -$10 |

$60 staked returned $150.96, **+$90.96 (+151.6%)**. Two hits against 1.10
expected; the model gave 22.4% odds of exactly two. Every fade held: Alonso
0-for-5, Schwarber 0-for-4, Henderson 0-for-5, Murakami 0-for-4.

One slate proves nothing about EV. It does, however, produce two findings that
change the design, each named for the play that produced it.

### Why the grader has a deadline

Calibration is empirical: it can only learn from outcomes recorded against
pre-slate snapshots. Grading currently happens inside the live refresh
(`build_player_hr_results` reads the game feed), and the last refresh each day
lands ~7:20 PM ET — mid-slate. Result: **4,459 of 4,521 entries were "pending."**
The season's outcomes had to be reconstructed by hand from the MLB Stats API.

NFL opens 2026-09-09. CFB is already playing. Slates that pass without
pre-game snapshots and post-game grading are lost permanently — they cannot be
reconstructed later. **The grader must be live before 09-09.**

## Goals

- Publish a calibrated probability, empirically fit against graded outcomes,
  for every sport and market.
- Weight the real market into the published probability, scaled by liquidity.
- Compute EV against real prices and drive BET/PASS from it deterministically.
- Report discrimination (AUC) per market so a market with no signal is visibly
  labeled as one.
- Model correlation for joint probabilities, and report portfolio exposure
  separately from EV.
- Grade every slate automatically, starting immediately.

## Non-goals

- Order execution. Everything stays REPORT_ONLY, matching the existing
  `kalshi_edge.py` convention.
- Changing the sim, the collectors, or the Vector Index scoring. This layer sits
  after board build and annotates.
- Bankroll sizing / staking. Exposure is *reported*; sizing is a later spec.
- CFB as a sport (spec 3) and NFL market wiring (spec 2).

## Architecture

A post-processing annotation layer. The board builds as it does today; new units
annotate it. The diagram shows the persistence path; the EV engine (5) and
correlation module (6) run inside the annotate step. Follows the additive pattern `kalshi_edge.py` established —
total failure of any unit leaves fields null and never breaks a board build.

```
board build ──▶ [4. annotate] ──▶ published board
                      ▲
                      │ reads
          data_final/calibration/<sport>.json
                      ▲
                      │ nightly refit
        [3. fitter] ◀── data_final/history/graded/<sport>/<date>.json
                                      ▲
                                      │ writes
        [1. grader] ◀── post-slate job (~4 AM ET, next day)

        [2. snapshotter] ──▶ data_final/history/board/<sport>/<date>.json
                             (pre-slate, immutable, the calibration input)
```

### Unifying insight

Every market reduces to a binary at a threshold. `HR 1+`, `TD 1+`,
`PTS over 24.5`, `RecYds over 62.5` are all *"did X cross T."* One calibration
engine covers all 18 distinct markets across every sport. Only the grading function
differs: box-score lookup for count markets, line comparison for continuous
ones. Continuous markets fit a curve per threshold bucket rather than one per
market.

## Components

### 1. `app/grading/` — outcome resolver

Runs as its own scheduled job the morning after a slate, decoupled from the
live refresh. This is the fix for the "pending" defect.

```
grade_slate(sport: str, date: str) -> GradedSlate
```

- Reads the immutable pre-slate snapshot for `(sport, date)`.
- Per selection, resolves the actual outcome via the sport's stats adapter.
- Emits `hit: 0|1`, the raw stat, and `gradable: bool` (false when the player
  did not appear — those rows are excluded from fitting, not counted as misses).
- Writes `data_final/history/graded/<sport>/<date>.json`. Idempotent; re-running
  overwrites cleanly.

Per-sport adapters implement one interface:

```
class StatsAdapter(Protocol):
    def resolve(self, player_id: str, date: str) -> dict | None: ...
```

MLB adapter uses the MLB Stats API game log (`stats=gameLog&group=hitting`),
already the collector's source. NFL/NBA/WNBA adapters follow the same shape
against their existing collectors' feeds.

### 2. `app/outputs/board_snapshot.py` — pre-slate snapshotter

`mlb_hr_tracking.py` already does a narrow version of this (HR only, MLB only,
5 files actually on disk). Generalize it:

- Writes one immutable snapshot per `(sport, date)` at the last pre-slate
  refresh, covering **all markets**, not just HR.
- Records `captured_at_et` and refuses to overwrite an existing file — the
  snapshot must be the pre-game state or the calibration leaks.
- Selection identity: `(sport, date, game_id, player_id, market, threshold)`.

Backfill: extend `scripts/backfill_mlb_hr_tracking.py` into a sport-agnostic
`scripts/backfill_board_history.py`. MLB and WNBA have deep git history
(1,761 and 1,441 commits) and can be reconstructed retroactively.

### 3. `app/scoring/empirical_calibration.py` — fitter

Per `(sport, market, threshold_bucket)`:

- Bins graded history by model probability (8 bins, equal count).
- Fits `actual = a + b · model` by least squares over bin midpoints.
- Computes AUC, sample count `n`, and the observed actual range across bins.
- Writes `data_final/calibration/<sport>.json`, versioned with `fit_at` and the
  date range consumed.

Applying the fit outside its fitted domain is the failure mode that produced
absurd results in exploration (a contact hitter with 6 HR in 557 PA scoring as
a 15% shot). The applier therefore clamps to the fitted domain and, below it,
applies the lowest bin's multiplicative ratio rather than extrapolating the
intercept.

Reference fit, MLB HR 1+, n=4,431: `actual = 10.38 + 0.364 · model`,
AUC 0.532, observed range 13.2%–20.0%.

### 4. Applier + market weighting

Annotates each selection with three probabilities and their provenance:

| Field | Meaning |
|---|---|
| `p_model_cal` | Calibrated model probability |
| `p_market` | De-vigged market implied, from the Kalshi bid/ask mid |
| `p_blend` | Liquidity-weighted blend — **the published true probability** |
| `w_market` | The weight actually applied |

```
p_blend = w_market · p_market + (1 - w_market) · p_model_cal
w_market = L / (L + k)
```

`L` is a liquidity score from volume, open interest, and inverse bid-ask width.
`k` is fit per `(sport, market)` by minimizing Brier score on graded history.
A thin market is down-weighted toward the model; a liquid one dominates. Where
only one side quotes, widen the implied interval and down-weight accordingly.

All four fields are published so a reader can always see where the number came
from.

### 5. `app/scoring/expected_value.py` — EV engine

```
ev = p · (decimal_odds - 1) - (1 - p)
breakeven_prob = 1 / decimal_odds
```

Emits `ev_pct`, `breakeven_prob`, `fair_odds`, and `decision`.

**The anchoring trap, handled explicitly.** Blending the market in and then
computing EV against that same market shrinks the measured edge toward zero by
construction — you cannot beat a price you are anchored to. Two EVs are emitted:

- **`ev_blend`** — drives BET/PASS. Conservative by design. This is the gate
  that stops Schwarber.
- **`ev_model_only`** — logged, never surfaced as a recommendation. Tracks
  whether the model adds information beyond the price.

Comparing realized outcomes on the two over a season answers whether the model
contributes anything the market does not. Given AUC 0.515, the honest prior is
that it largely does not; the blend protects the operator while that is
established.

Price sources: Kalshi today (`KXMLBHR` ladder is live with 318 markets), plus a
manual-odds override slot for books Kalshi does not cover. Missing price leaves
`ev: null`, never an exception.

### 6. `app/scoring/correlation.py`

Two outputs, never conflated.

**EV of a set of singles is additive regardless of dependence.** Correlation
changes variance, not expected value. This distinction is the point of the
module.

1. **Joint probability engine** — ρ estimated per `(sport, correlation_class)`
   from graded history. Classes: same-game, same-team, same-opposing-pitcher
   (MLB), same-QB and same-game-script (NFL/CFB). Used for multi-leg tickets.
2. **Portfolio exposure report** — flags concentration across singles, e.g.
   *"3 selections, 78% of variance on one pitcher."* A risk statement, not an
   EV adjustment.

**Significance gate.** ρ is published with its sample size and z-score, and
defaults to independence until it clears 95%. The same bar governs every
h2h-driven adjustment including exclusion - see The Crow-Armstrong Gate. Measured MLB same-team same-day:
92 co-occurrences vs 81.5 expected across 2,792 pairs — **1.13x, z = +1.18, not
significant**. MLB HR would ship as independent today. NFL passing-game
correlation is expected to be far stronger and is the primary consumer of this
module.

## The Cowser Rule - candidate discovery

**Colton Cowser was never on the board.** The 2026-08-29 HR board listed four
Orioles against Jack Perkins: Encarnacion-Strand, Alonso, Basallo, Henderson.
All four lost. Cowser hit, at +540, and he entered the analysis only because the
full opposing lineup was priced by hand off a sportsbook screen.

A system that ranks only what the board already selected cannot find him. Ranking
is not discovery.

**Rule.** When a probable pitcher clears a vulnerability threshold for a market,
expand the candidate set to the **entire opposing lineup** and price every batter,
regardless of whether the board surfaced them.

- Vulnerability threshold is per market, from the same shrunk pitcher rate the
  applier already computes (for HR: shrunk HR/9 at or above league).
- Expanded candidates carry `discovery: "lineup_expansion"` so their hit rate can
  be tracked separately against board-native selections.
- Expansion respects the same guardrails as any other candidate: established-role
  filter, minimum season sample, and the cold-start source flag.

The measurable question this answers over a season: do expanded candidates
outperform board-native ones at the same price? On the founding slate the board's
four Orioles went 0-for-4 and the expanded lineup produced the winner.

## The Crow-Armstrong Gate - no veto on an insignificant sample

**Pete Crow-Armstrong went 4-for-6 with 3 HR and 13 total bases.** He had been
excluded on a head-to-head record of 0-for-13 with 6 K against Andrew Abbott -
the single most confident exclusion of the slate.

That was a process error, not variance. At a 5% per-PA home-run rate, observing
zero home runs in 14 plate appearances has probability 0.49. It is a coin flip.
It was treated as disqualifying.

**Rule.** The significance gate is not limited to the joint-probability module.
**Any** head-to-head-driven adjustment must clear it - including exclusion.

- A h2h sample may adjust or veto only when it reaches significance against the
  relevant base rate for that market.
- Below the bar the sample is reported for display and contributes nothing to
  probability, ranking, or eligibility.
- Vetoes are logged with their z-score so wrongful exclusions are auditable after
  the fact.

Under this gate, Crow-Armstrong's 14 PA could not have removed him.

## Decision rules v2

Replaces the current `edge_pp`-on-raw-sim rule. Fully determined; no
discretionary step:

```
BET  if  ev_blend >= min_ev
     and calibration.source == "empirical"
     and calibration.n >= 300
     and market_liquidity >= liquidity_floor
     and portfolio_exposure <= exposure_cap
PASS otherwise
```

Gate values are config, not magic numbers, and live per `(sport, market)`:
`min_ev` starts at +5.0% EV, `liquidity_floor` at 50 contracts of resting size,
`exposure_cap` at 40% of a slate's total staked variance on any one
correlation class. Each is tunable without a code change.

Every gate is a computed, displayable number. The Schwarber case fails the EV
gate mechanically rather than requiring a human to notice.

## Cold start

`calibration.source` is `empirical` once a `(sport, market, threshold)` has
≥300 gradable rows; otherwise `closed_form`, falling back to the existing
`calibration_guardrail.py` baselines (binomial for hits, `1-(1-p)^PA` for HR,
Poisson for K).

The threshold is rows, not slates. NFL runs ~16 games × ~10 props ≈ 160 rows
per week and clears 300 in roughly two weeks; CFB clears faster on volume. No
football-specific loosening is needed.

`calibration.source`, `n`, and `auc` are published on every row so the board can
state its own confidence.

## Data contracts

Annotation added to each selection (additive; `sim_prob_pct` is retained
unchanged as the fitter's input):

```json
{
  "p_model_cal": 0.205,
  "p_market": 0.115,
  "p_blend": 0.163,
  "w_market": 0.62,
  "ev_blend": 0.090,
  "ev_model_only": 0.163,
  "breakeven_prob": 0.115,
  "fair_odds": "+513",
  "decision": "BET",
  "calibration": {
    "source": "empirical",
    "a": 10.38, "b": 0.364,
    "n": 4431, "auc": 0.532,
    "observed_range": [0.132, 0.200],
    "fit_at": "2026-08-29T04:00:00Z"
  }
}
```

Graded row:

```json
{
  "sport": "MLB", "date": "2026-08-29", "game_id": "bal-ath-2026-08-29",
  "player_id": "624413", "market": "HR", "threshold": 1,
  "model_prob": 0.260, "p_blend": 0.205,
  "gradable": true, "hit": 0, "raw_stat": {"hr": 0, "pa": 4}
}
```

## Error handling

Follows the established additive convention — no path breaks a board build.

| Failure | Behavior |
|---|---|
| Grader cannot resolve a player | `gradable: false`, excluded from fitting, retried next run |
| Stats API unavailable | Slate left ungraded, retried; never writes a partial file |
| No calibration curve for a market | Falls back to `closed_form`, flagged in `source` |
| Kalshi unavailable | `p_market`, `ev` null; `p_blend` degrades to `p_model_cal` |
| ρ below significance | Defaults to independence |
| Snapshot already exists for date | Refuses overwrite (leak prevention), logs and continues |

## Testing

- **Fitter** recovers known `(a, b)` from a synthetic curve within tolerance.
- **Grader** against fixture feeds, including the did-not-play case.
- **EV math** against hand-computed cases: Cowser +540 at 20.0% → +28%;
  Schwarber +176 at 20.5% → −43%.
- **Blend** — verify `w_market → 1` as liquidity grows, `→ 0` as it vanishes.
- **Cold-start switchover** at exactly n=300.
- **Domain clamping** — a low-power hitter far below the fitted domain must not
  be lifted by the intercept.
- **Correlation significance gate** — ρ below 95% must yield independence.
- **Regression test against committed graded history:** MLB HR top-decile must
  land at 20.8% ± tolerance. A refactor that breaks calibration fails CI.

## Sequencing

1. **Snapshotter + grader** — before 2026-09-09. Nothing else can be fit
   without them, and unrecorded slates are lost permanently.
2. Backfill MLB and WNBA from git history.
3. Fitter + applier + cold start.
4. Market weighting + EV engine + decision rules v2.
5. Correlation + exposure.

## Related specs

| Spec | Contents |
|---|---|
| **1. This document** | Grading, calibration, market weighting, EV, correlation. Adapters for MLB/WNBA/NBA/NFL. |
| **2. Football market wiring** | Enable the NFL `KXNFLGAME` join (the series constant exists in `kalshi_connector.py` but is unused — NFL boards currently show `kalshi: null`); discover and map NFL prop ladder series. |
| **3. CFB sport** | New collector, model, board builder, Kalshi series. CFB is entirely absent today — no model, collector, or data file. |

The core must let spec 3 drop in as an adapter with zero changes to the
calibration engine. That is the coupling test this design has to pass.

## Open questions

- Kalshi de-vigging method for one-sided quotes needs validation against
  observed fills.
- Whether `k` in the liquidity weight is stable enough to fit per market, or
  should start as a single global constant per sport.
