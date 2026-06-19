from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.main import run_mlb_pipeline, run_nba_pipeline, run_soccer_pipeline, run_tennis_pipeline, run_wnba_pipeline
from app.outputs.picks_snapshot import write_picks_snapshot
from app.paths import build_paths

# Each sport runs in isolation. A single collector throwing (e.g. an upstream
# data API blocking the CI runner) must NOT abort the entire refresh — the
# remaining sports still rebuild and the run still commits. The whole job only
# fails if every sport fails, which is a real outage worth surfacing.
PIPELINES = (
    ("mlb", run_mlb_pipeline),
    ("nba", run_nba_pipeline),
    ("wnba", run_wnba_pipeline),
    ("soccer", run_soccer_pipeline),
    ("tennis", run_tennis_pipeline),
)


def main() -> None:
    repo_root = PROJECT_ROOT.parent

    boards: dict[str, dict] = {}
    failures: dict[str, str] = {}

    for name, run_pipeline in PIPELINES:
        try:
            boards[name] = run_pipeline(repo_root)
            print(f"[run_all] {name}: ok", flush=True)
        except Exception as exc:  # noqa: BLE001 - isolate per-sport failures
            failures[name] = f"{type(exc).__name__}: {exc}"
            print(f"[run_all] {name}: FAILED -> {failures[name]}", file=sys.stderr, flush=True)
            traceback.print_exc()

    if boards:
        try:
            write_picks_snapshot(boards=boards, paths=build_paths(repo_root))
            print(f"[run_all] picks snapshot written ({', '.join(boards)})", flush=True)
        except Exception:  # noqa: BLE001
            print("[run_all] picks snapshot FAILED", file=sys.stderr, flush=True)
            traceback.print_exc()

    if failures:
        print(
            f"[run_all] completed with {len(failures)} sport failure(s): "
            f"{', '.join(f'{k} ({v})' for k, v in failures.items())}",
            file=sys.stderr,
            flush=True,
        )

    if not boards:
        # Nothing rebuilt at all — a genuine outage. Fail the job so it's visible.
        print("[run_all] ALL sports failed — failing the run.", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
