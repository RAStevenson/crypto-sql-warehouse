from __future__ import annotations

import sys
from pathlib import Path

# Same bootstrap as scripts/run_*.py: put src/ on the import path so a fresh
# clone runs the tests with no install step.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
