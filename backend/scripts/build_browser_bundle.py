from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.outputs.browser_bundle import write_browser_bundle
from app.paths import build_paths


if __name__ == "__main__":
    write_browser_bundle(build_paths(PROJECT_ROOT.parent))
