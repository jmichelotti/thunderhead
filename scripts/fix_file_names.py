#!/usr/bin/env python3
"""
fix_file_names.py

Runs:
  1) fix_tv_names.py --apply
  2) fix_movie_names.py --apply

- Assumes all scripts are in the same folder as this file.
- Exits immediately if either script fails.
- Forwards any extra CLI args you pass to BOTH scripts (optional).
  Example:
    python fix_file_names.py --verbose
  will run:
    fix_tv_names.py --apply --verbose
    fix_movie_names.py --apply --verbose
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run(script_path: Path, extra_args: list[str]) -> None:
    cmd = [sys.executable, str(script_path), "--apply", *extra_args]
    print(f"\n=== Running: {' '.join(cmd)} ===")
    subprocess.run(cmd, check=True)


def main() -> int:
    here = Path(__file__).resolve().parent
    tv_script = here / "fix_tv_names.py"
    movie_script = here / "fix_movie_names.py"

    missing = [p.name for p in (tv_script, movie_script) if not p.exists()]
    if missing:
        print("ERROR: Missing script(s) next to fix_file_names.py:", ", ".join(missing))
        return 1

    extra_args = sys.argv[1:]  # forwarded to both scripts (optional)

    try:
        run(tv_script, extra_args)
        run(movie_script, extra_args)
    except subprocess.CalledProcessError as e:
        print(f"\nERROR: A script failed with exit code {e.returncode}")
        return e.returncode

    print("\nAll done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
