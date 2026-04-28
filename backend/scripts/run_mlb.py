from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.main import run_mlb_pipeline


if __name__ == "__main__":
    run_mlb_pipeline(PROJECT_ROOT.parent)
