"""Run the construction-gauge production preflight.

Usage:
    uv run python scripts/preflight_construction_outcome_effects.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    commands = [
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_frontend_search_contract.py",
            "-k",
            "construction_outcome",
            "-q",
        ],
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "tests/test_frontend_search_contract.py",
            "scripts/preflight_construction_outcome_effects.py",
        ],
    ]
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
