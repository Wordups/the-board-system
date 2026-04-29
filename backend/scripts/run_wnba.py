from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.main import run_wnba_pipeline


def main() -> None:
    repo_root = PROJECT_ROOT.parent
    run_wnba_pipeline(repo_root)


if __name__ == "__main__":
    main()
