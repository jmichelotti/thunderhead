#!/usr/bin/env python3
"""
master_jf_operations.py

Runs the full Jellyfin media pipeline in order:
  1. fix_metadata_for_jellyfin.py --apply
  2. fix_file_names.py  (which runs fix_tv_names + fix_movie_names with --apply)
  3. migrate_files.py   (dry run first, then apply if approved)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(script: Path, args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(script), *args]
    print(f"\n{'='*60}")
    print(f"Running: {' '.join(cmd)}")
    print("=" * 60)
    return subprocess.run(cmd, capture_output=capture, text=capture)


def main() -> int:
    metadata_script = HERE / "fix_metadata_for_jellyfin.py"
    naming_script = HERE / "fix_file_names.py"
    migrate_script = HERE / "migrate_files.py"

    missing = [s.name for s in (metadata_script, naming_script, migrate_script) if not s.exists()]
    if missing:
        print(f"ERROR: Missing script(s): {', '.join(missing)}")
        return 1

    # 1. Fix metadata
    result = run(metadata_script, ["--apply"])
    if result.returncode != 0:
        print(f"\nERROR: fix_metadata_for_jellyfin.py failed (exit {result.returncode})")
        return result.returncode

    # 2. Fix file names
    result = run(naming_script, [])
    if result.returncode != 0:
        print(f"\nERROR: fix_file_names.py failed (exit {result.returncode})")
        return result.returncode

    # 3. Migrate files — dry run
    print(f"\n{'='*60}")
    print("Running migration dry run...")
    print("=" * 60)
    result = run(migrate_script, [], capture=True)
    if result.returncode != 0:
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        print(f"\nERROR: migrate_files.py dry run failed (exit {result.returncode})")
        return result.returncode

    print(result.stdout)

    # 4. Approve or deny
    try:
        answer = input("\nApprove migration? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 0

    if answer != "y":
        print("Migration cancelled.")
        return 0

    # 5. Migrate files — apply
    result = run(migrate_script, ["--apply"])
    if result.returncode != 0:
        print(f"\nERROR: migrate_files.py failed (exit {result.returncode})")
        return result.returncode

    print("\nAll done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
