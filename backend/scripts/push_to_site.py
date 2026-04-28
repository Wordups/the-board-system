from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.publishing.publish_board import publish


if __name__ == "__main__":
    publish(PROJECT_ROOT.parent, "Update board JSON")
