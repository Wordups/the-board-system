"""Backtest harness: replay historical board snapshots, resolve real
outcomes, and report the model's actual calibration.

Usage (from repo root):

    python backend/backtest/harness.py --sports mlb,wnba,nba \
        --start 2026-04-29 --end 2026-07-17

Everything fetched from the network (MLB StatsAPI, ESPN, Kalshi) is cached
under backend/backtest/cache/ — after one full run the whole backtest
replays with --offline.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.builders.kalshi_edge import decide_pick  # noqa: E402

from backtest import calibration, kalshi_history, outcomes_espn, outcomes_mlb  # noqa: E402
from backtest.snapshots import (  # noqa: E402
    PREGAME_CUTOFF_ET,
    extract_picks,
    list_snapshot_commits,
    load_board,
    select_daily_snapshots,
)

PROP_MARKETS_MLB = set(outcomes_mlb.STAT_FOR_MARKET)
PROP_MARKETS_ESPN = set(outcomes_espn.LABEL_FOR_MARKET)


def run_sport(
    sport: str,
    *,
    repo_root: str,
    start: str | None,
    end: str | None,
    offline: bool,
    stake: float,
) -> dict[str, Any]:
    rel_path = f"data/{sport}.json"
    commits = list_snapshot_commits(repo_root, rel_path)
    daily = select_daily_snapshots(
        commits, cutoff_et_hour=PREGAME_CUTOFF_ET.get(sport, 12), start=start, end=end
    )

    resolved: list[dict[str, Any]] = []
    void = 0
    stale_days: list[str] = []
    fallback_days = 0
    dates_used: list[str] = []
    snapshot_ts_by_date: dict[str, int] = {}

    for day in daily:
        board = load_board(repo_root, day["hash"], rel_path)
        if board is None or str(board.get("date")) != day["date"]:
            stale_days.append(day["date"])
            continue
        if not day["pregame"]:
            fallback_days += 1
        picks = extract_picks(board, sport=sport, date=day["date"])
        if not picks:
            continue
        dates_used.append(day["date"])
        snapshot_ts_by_date[day["date"]] = int(day["ts"].timestamp())

        if sport == "mlb":
            results = outcomes_mlb.day_results(day["date"], offline=offline)
            for pick in picks:
                if results is None:
                    outcome = None
                elif pick["market"] == "ML":
                    outcome = outcomes_mlb.resolve_moneyline(pick, results)
                elif pick["market"] in PROP_MARKETS_MLB:
                    outcome = outcomes_mlb.resolve_prop(pick, results)
                else:
                    outcome = None
                if outcome is None:
                    void += 1
                else:
                    resolved.append({**pick, "outcome": outcome})
        else:
            summaries: dict[str, Any] = {}
            for pick in picks:
                event_id = pick["game_id"]
                if event_id not in summaries:
                    summaries[event_id] = outcomes_espn.event_summary(
                        sport, event_id, offline=offline
                    )
                summary = summaries[event_id]
                if summary is None:
                    outcome = None
                elif pick["market"] == "ML":
                    outcome = outcomes_espn.resolve_moneyline(pick, summary)
                elif pick["market"] in PROP_MARKETS_ESPN:
                    outcome = outcomes_espn.resolve_prop(pick, summary)
                else:
                    outcome = None
                if outcome is None:
                    void += 1
                else:
                    resolved.append({**pick, "outcome": outcome})

    report: dict[str, Any] = {
        "sport": sport,
        "days_selected": len(daily),
        "days_used": len(dates_used),
        "date_range": (dates_used[0], dates_used[-1]) if dates_used else (None, None),
        "fallback_days": fallback_days,
        "stale_days": stale_days,
        "resolved": resolved,
        "void": void,
    }
    if sport == "mlb":
        report["decision_replay"] = replay_ml_decisions(
            resolved, snapshot_ts_by_date, offline=offline, stake=stake
        )
    return report


def replay_ml_decisions(
    resolved: list[dict[str, Any]],
    snapshot_ts_by_date: dict[str, int],
    *,
    offline: bool,
    stake: float,
) -> dict[str, Any]:
    """Stamp historical ML picks with what the decision layer WOULD have said,
    using Kalshi pregame candlestick prices, and P&L flat stakes on BETs."""
    ml_picks = [p for p in resolved if p["market"] == "ML"]
    lookup = kalshi_history.settled_lookup(
        sorted({p["date"] for p in ml_picks}), offline=offline
    )

    matched = 0
    priced = 0
    post_snapshot_prices = 0
    result_disagreements = 0
    stamps = {"BET": 0, "PASS": 0, "CHECK": 0}
    replayed_bets: list[dict[str, Any]] = []
    recorded_bets: list[dict[str, Any]] = []
    recorded_stamps: dict[str, int] = {}

    for pick in ml_picks:
        decision_rec = pick.get("recorded_decision")
        if decision_rec:
            recorded_stamps[decision_rec] = recorded_stamps.get(decision_rec, 0) + 1
            if decision_rec == "BET" and pick.get("recorded_implied_prob"):
                recorded_bets.append(
                    {"implied_prob": pick["recorded_implied_prob"], "outcome": pick["outcome"]}
                )

        away, home = outcomes_mlb.pick_away(pick), outcomes_mlb.pick_home(pick)
        market = lookup.get((pick["date"], away, home, pick["team"]))
        if market is None:
            continue
        matched += 1
        if int(market["won"]) != int(pick["outcome"]):
            result_disagreements += 1

        price = kalshi_history.pregame_price(
            market["ticker"], snapshot_ts_by_date[pick["date"]], offline=offline
        )
        if price is None:
            continue
        priced += 1
        if price["post_snapshot"]:
            post_snapshot_prices += 1
        implied = price["implied_prob"]
        edge_pp = (pick["model_prob"] - implied) * 100.0
        decision = decide_pick(edge_pp, implied)
        stamps[decision] += 1
        if decision == "BET":
            replayed_bets.append(
                {
                    "date": pick["date"],
                    "team": pick["team"],
                    "implied_prob": implied,
                    "model_prob": pick["model_prob"],
                    "edge_pp": round(edge_pp, 1),
                    "outcome": pick["outcome"],
                }
            )

    return {
        "ml_picks": len(ml_picks),
        "kalshi_matched": matched,
        "kalshi_priced": priced,
        "post_snapshot_prices": post_snapshot_prices,
        "result_disagreements": result_disagreements,
        "replayed_stamps": stamps,
        "replayed_bets": replayed_bets,
        "replayed_pnl": calibration.flat_stake_pnl(replayed_bets, stake),
        "recorded_stamps": recorded_stamps,
        "recorded_pnl": calibration.flat_stake_pnl(recorded_bets, stake),
    }


# ---------------------------------------------------------------- reporting


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100.0:.1f}%"


def _fmt_summary_row(name: str, stats: dict[str, Any]) -> str:
    return (
        f"| {name} | {stats['n']} | {_pct(stats['avg_model_prob'])} "
        f"| {_pct(stats['hit_rate'])} | {stats['gap_pp'] if stats['gap_pp'] is not None else '-'} "
        f"| {stats['brier'] if stats['brier'] is not None else '-'} |"
    )


SUMMARY_HEADER = (
    "| bucket | n | avg model prob | hit rate | gap (pp) | Brier |\n"
    "|---|---|---|---|---|---|"
)


def render_sport_report(report: dict[str, Any], stake: float) -> str:
    sport = report["sport"].upper()
    resolved = report["resolved"]
    lines = [f"## {sport}"]
    lo, hi = report["date_range"]
    lines.append(
        f"- history: {report['days_used']} board days used ({lo} .. {hi}), "
        f"{report['days_selected']} days selected, {len(report['stale_days'])} skipped stale, "
        f"{report['fallback_days']} without a pregame snapshot (earliest-of-day used)"
    )
    lines.append(
        f"- picks resolved: {len(resolved)} (void/unresolvable: {report['void']})"
    )
    if not resolved:
        return "\n".join(lines) + "\n"

    lines.append("\n### Calibration by market bucket\n" + SUMMARY_HEADER)
    for market, stats in calibration.bucket_table(resolved).items():
        lines.append(_fmt_summary_row(market, stats))
    lines.append(_fmt_summary_row("ALL", calibration.summarize(resolved)))

    lines.append("\n### Calibration by model-probability decile\n" + SUMMARY_HEADER)
    for row in calibration.decile_table(resolved):
        if row["n"] == 0:
            continue
        lines.append(_fmt_summary_row(f"{row['lo']:.1f}-{row['hi']:.1f}", row))

    replay = report.get("decision_replay")
    if replay:
        lines.append("\n### Moneyline decision replay (Kalshi historical prices)")
        lines.append(
            f"- {replay['ml_picks']} resolved ML picks; {replay['kalshi_matched']} matched a "
            f"settled Kalshi market; {replay['kalshi_priced']} had a recoverable pregame price "
            f"({replay['post_snapshot_prices']} priced from the first post-snapshot candle); "
            f"{replay['result_disagreements']} Kalshi-vs-boxscore result disagreements"
        )
        stamps = replay["replayed_stamps"]
        lines.append(
            f"- replayed stamps: BET {stamps['BET']} / PASS {stamps['PASS']} / CHECK {stamps['CHECK']}"
        )
        pnl = replay["replayed_pnl"]
        if pnl["n"]:
            lines.append(
                f"- flat ${stake:.0f} on replayed BET stamps: {pnl['n']} bets, "
                f"{pnl['wins']}W-{pnl['losses']}L, staked ${pnl['staked']}, "
                f"P&L ${pnl['pnl']} (ROI {_pct(pnl['roi'])})"
            )
            bet_calib = calibration.summarize(
                [{"model_prob": b["model_prob"], "outcome": b["outcome"]} for b in replay["replayed_bets"]]
            )
            market_calib = calibration.summarize(
                [{"model_prob": b["implied_prob"], "outcome": b["outcome"]} for b in replay["replayed_bets"]]
            )
            lines.append(
                f"- on those bets: model said {_pct(bet_calib['avg_model_prob'])}, market said "
                f"{_pct(market_calib['avg_model_prob'])}, actual {_pct(bet_calib['hit_rate'])}"
            )
        if replay["recorded_stamps"]:
            rec = ", ".join(f"{k} {v}" for k, v in sorted(replay["recorded_stamps"].items()))
            rec_pnl = replay["recorded_pnl"]
            lines.append(f"- recorded stamps in snapshots (live since 2026-07-17): {rec}")
            if rec_pnl["n"]:
                lines.append(
                    f"- flat ${stake:.0f} on recorded BET stamps: {rec_pnl['n']} bets, "
                    f"{rec_pnl['wins']}W-{rec_pnl['losses']}L, P&L ${rec_pnl['pnl']} "
                    f"(ROI {_pct(rec_pnl['roi'])})"
                )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Board-system backtest harness")
    parser.add_argument("--sports", default="mlb,wnba,nba")
    parser.add_argument("--start", default=None, help="first board date (ET), YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="last board date (ET); default yesterday")
    parser.add_argument("--stake", type=float, default=5.0)
    parser.add_argument("--offline", action="store_true", help="cache only, no network")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--out", default=None, help="also write the report to this file")
    args = parser.parse_args(argv)

    end = args.end or (datetime.now(timezone.utc) - timedelta(hours=28)).strftime("%Y-%m-%d")

    sections = [
        "# Backtest calibration report",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | "
        f"window: {args.start or 'history start'} .. {end} | "
        "snapshot policy: last board refresh at/before the sport's pregame cutoff "
        "(ET); outcome sources: MLB StatsAPI boxscores, ESPN summaries, Kalshi "
        "settled markets + candlesticks.",
        "Gap (pp) = avg model prob - actual hit rate: positive means the model "
        "OVERSTATES its chances. Brier: 0 perfect, 0.25 = coin-flip forecaster.",
        "",
    ]
    all_resolved: list[dict[str, Any]] = []
    for sport in [s.strip().lower() for s in args.sports.split(",") if s.strip()]:
        report = run_sport(
            sport,
            repo_root=args.repo_root,
            start=args.start,
            end=end,
            offline=args.offline,
            stake=args.stake,
        )
        all_resolved.extend(report["resolved"])
        sections.append(render_sport_report(report, args.stake))

    if all_resolved:
        sections.append("## ALL SPORTS - pooled deciles\n" + SUMMARY_HEADER)
        for row in calibration.decile_table(all_resolved):
            if row["n"]:
                sections.append(_fmt_summary_row(f"{row['lo']:.1f}-{row['hi']:.1f}", row))
        sections.append("")

    text = "\n".join(sections)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
