#!/usr/bin/env python
"""
migrate_files.py

Move/merge TV + Movie libraries from multiple "old" roots into new roots.

TV behavior (drive-aware):
1) If show exists in D:\TV Shows -> merge there
2) Else if show exists in F:\TV Shows -> merge there
3) Else -> create show in L:\TV Shows

Movies behavior:
- Treats each immediate child folder of an OLD_MOVIE_DIR as a "movie folder"
- Moves movie folders into NEW_MOVIE_DIR
- If destination exists, merges contents

Safe by default: DRY RUN unless you pass --apply.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Iterable


# =========================
# CONFIG
# =========================

OLD_TV_DIRS = [
    r"C:\Temp_Media\TV Shows",
]

OLD_MOVIE_DIRS = [
    r"C:\Temp_Media\Movies",
]

# TV destinations (checked in order for existing shows; new shows go to NEW_TV_DIR)
NEW_TV_DIR = r"L:\TV Shows"         # new shows land here
EXISTING_TV_DIRS = [
    r"D:\TV Shows",
    r"F:\TV Shows",
]

# Movie destination
NEW_MOVIE_DIR = r"L:\Movies"


# =========================
# LOGGING
# =========================

def log(msg: str) -> None:
    print(msg)

def v_log(msg: str, verbose: bool) -> None:
    if verbose:
        print(msg)


# =========================
# HELPERS
# =========================

def ensure_dir(p: Path, dry_run: bool, verbose: bool) -> None:
    if dry_run:
        if p.exists():
            v_log(f"[DRY RUN] Dir exists: {p}", verbose)
        else:
            log(f"[DRY RUN] Would create dir: {p}")
        return
    p.mkdir(parents=True, exist_ok=True)

def iter_immediate_children_dirs(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for child in root.iterdir():
        if child.is_dir():
            yield child

def move_path(src: Path, dst: Path, dry_run: bool, verbose: bool) -> None:
    if dry_run:
        log(f"[DRY RUN] Would move:\n  {src}\n  -> {dst}")
        return
    shutil.move(str(src), str(dst))
    v_log(f"[Moved] {src} -> {dst}", verbose)

def unique_file_path(dst: Path) -> Path:
    if not dst.exists():
        return dst
    base = dst.with_suffix("")
    suffix = dst.suffix
    n = 1
    while True:
        candidate = Path(f"{base} (migrated {n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1

def count_files_under(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())

def merge_dirs(src_dir: Path, dst_dir: Path, dry_run: bool, verbose: bool) -> None:
    ensure_dir(dst_dir, dry_run, verbose)

    total_files = count_files_under(src_dir)
    moved_files = 0

    log(f"    Merging {total_files} file(s) from:\n      {src_dir}\n    into:\n      {dst_dir}")

    for src_path in src_dir.rglob("*"):
        rel = src_path.relative_to(src_dir)
        dst_path = dst_dir / rel

        if src_path.is_dir():
            ensure_dir(dst_path, dry_run, verbose)
            continue

        ensure_dir(dst_path.parent, dry_run, verbose)

        final_dst = dst_path
        if final_dst.exists():
            final_dst = unique_file_path(final_dst)

        moved_files += 1
        log(f"      [{moved_files}/{total_files}] {rel}")
        move_path(src_path, final_dst, dry_run, verbose)

    if dry_run:
        log(f"    [DRY RUN] Would remove empty dirs under: {src_dir}")
        return

    for d in sorted([p for p in src_dir.rglob("*") if p.is_dir()], reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass

    try:
        src_dir.rmdir()
    except OSError:
        pass


# =========================
# TV-SPECIFIC LOGIC
# =========================

def choose_tv_destination(show_name: str, existing_dirs: list[Path], new_dir: Path) -> Path:
    for d in existing_dirs:
        candidate = d / show_name
        if candidate.exists():
            return candidate

    return new_dir / show_name


def migrate_tv_library(old_roots: list[str], dry_run: bool, verbose: bool) -> None:
    new_dir = Path(NEW_TV_DIR)
    existing_dirs = [Path(d) for d in EXISTING_TV_DIRS]

    ensure_dir(new_dir, dry_run, verbose)

    planned = []
    for old in old_roots:
        root = Path(old)
        if root.exists():
            planned.extend(iter_immediate_children_dirs(root))

    log("\n=== TV migration (drive-aware) ===")
    log(f"  New shows:    {new_dir}")
    log(f"  Existing:     {', '.join(str(d) for d in existing_dirs)}")
    log(f"  Found {len(planned)} show folder(s)")

    processed = 0

    for old in old_roots:
        root = Path(old)
        if not root.exists():
            continue

        log(f"\n  From: {root}")

        for show_dir in iter_immediate_children_dirs(root):
            processed += 1
            show_name = show_dir.name

            dst_dir = choose_tv_destination(show_name, existing_dirs, new_dir)

            log(f"\n  ({processed}/{len(planned)}) {show_name}")
            log(f"    Target: {dst_dir}")

            if dst_dir.exists():
                log("    [Merge]")
                merge_dirs(show_dir, dst_dir, dry_run, verbose)
            else:
                log("    [Move new]")
                move_path(show_dir, dst_dir, dry_run, verbose)


# =========================
# MOVIE LOGIC (UNCHANGED)
# =========================

def migrate_library(old_roots: list[str], new_root: str, kind: str, dry_run: bool, verbose: bool) -> None:
    new_root_p = Path(new_root)
    ensure_dir(new_root_p, dry_run, verbose)

    planned_items = []
    for old in old_roots:
        root = Path(old)
        if root.exists():
            planned_items.extend(iter_immediate_children_dirs(root))

    log(f"\n=== {kind} migration ===")
    log(f"  Destination: {new_root_p}")
    log(f"  Found {len(planned_items)} item folder(s)")

    processed = 0
    for old in old_roots:
        root = Path(old)
        if not root.exists():
            continue

        log(f"\n  From: {root}")

        for item_dir in iter_immediate_children_dirs(root):
            processed += 1
            dst_dir = new_root_p / item_dir.name

            log(f"\n  ({processed}/{len(planned_items)}) {item_dir.name}")

            if dst_dir.exists():
                log("    [Merge]")
                merge_dirs(item_dir, dst_dir, dry_run, verbose)
            else:
                log("    [Move new]")
                move_path(item_dir, dst_dir, dry_run, verbose)


# =========================
# MAIN
# =========================

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Merge/migrate TV + Movie folders.")
    p.add_argument("--apply", action="store_true", help="Actually move files/folders.")
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)

def main(argv: list[str]) -> int:
    args = parse_args(argv)
    dry_run = not args.apply
    verbose = args.verbose

    log("=== Media Migration ===")
    log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")

    migrate_tv_library(OLD_TV_DIRS, dry_run, verbose)
    migrate_library(OLD_MOVIE_DIRS, NEW_MOVIE_DIR, "MOVIES", dry_run, verbose)

    log("\nDone.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
