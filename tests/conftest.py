"""Put each skill's scripts/ directory on sys.path for import."""

import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / "skills" / "finance"

for name in ("options-chain", "options-strategy", "position-sizing", "trade-journal", "market-brief", "trailing-buy"):
    path = SKILLS / name / "scripts"
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
