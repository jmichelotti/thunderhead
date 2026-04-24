#!/usr/bin/env python
"""
Fix a misnamed TV show folder+files by looking up the correct title/year via IMDb ID.

Usage:
    python fix_show_year.py --imdb tt10168312 --path "L:\\TV Shows\\What If (2024)"
    python fix_show_year.py --imdb tt10168312 --path "L:\\TV Shows\\What If (2024)" --apply
"""

import argparse
import re
import shutil
from pathlib import Path

import requests

OMDB_API_KEY = "591dfd18"

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}


def sanitize_for_windows(name: str) -> str:
    invalid_chars = r'<>:"/\\|?*'
    name = name.translate(str.maketrans({ch: " " for ch in invalid_chars}))
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(" .")


def lookup_imdb(imdb_id: str) -> dict:
    r = requests.get("https://www.omdbapi.com/", params={
        "apikey": OMDB_API_KEY,
        "i": imdb_id,
        "type": "series",
    }, timeout=10)
    data = r.json()
    if data.get("Response") != "True":
        raise SystemExit(f"OMDb lookup failed for {imdb_id}: {data.get('Error', 'Unknown error')}")

    year = "".join(c for c in data.get("Year", "") if c.isdigit())[:4]
    if len(year) != 4:
        raise SystemExit(f"Could not parse year from OMDb response: {data.get('Year')}")

    title = sanitize_for_windows(data["Title"])
    return {"title": title, "year": year, "raw_title": data["Title"]}


def fix_show(show_path: Path, imdb_id: str, dry_run: bool) -> None:
    if not show_path.is_dir():
        raise SystemExit(f"Not a directory: {show_path}")

    meta = lookup_imdb(imdb_id)
    new_show_name = f"{meta['title']} ({meta['year']})"
    old_show_name = show_path.name

    print(f"OMDb result: {meta['raw_title']} ({meta['year']})")
    print(f"Old folder name: {old_show_name}")
    print(f"New folder name: {new_show_name}")
    print(f"Dry run: {dry_run}")
    print("=" * 60)

    if old_show_name == new_show_name:
        print("Folder name already correct, checking files...")

    # Rename files inside each season folder
    for season_dir in sorted(show_path.iterdir()):
        if not season_dir.is_dir():
            continue

        for f in sorted(season_dir.iterdir()):
            if f.suffix.lower() not in VIDEO_EXTS | SUB_EXTS:
                continue

            if old_show_name in f.name:
                new_name = f.name.replace(old_show_name, new_show_name)
                new_file = f.parent / new_name

                if dry_run:
                    print(f"[DRY RUN] {f.name}  ->  {new_name}")
                else:
                    f.rename(new_file)
                    print(f"Renamed: {f.name}  ->  {new_name}")

    # Rename the show folder itself
    new_show_path = show_path.parent / new_show_name
    if show_path != new_show_path:
        if new_show_path.exists():
            raise SystemExit(f"Target folder already exists: {new_show_path}")

        if dry_run:
            print(f"\n[DRY RUN] Folder: {show_path}  ->  {new_show_path}")
        else:
            show_path.rename(new_show_path)
            print(f"\nRenamed folder: {show_path}  ->  {new_show_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix TV show folder/file names using IMDb ID lookup")
    parser.add_argument("--imdb", required=True, help="IMDb ID (e.g. tt10168312)")
    parser.add_argument("--path", required=True, help="Path to the show folder to fix")
    parser.add_argument("--apply", action="store_true", help="Actually rename (default is dry-run)")
    args = parser.parse_args()

    fix_show(Path(args.path), args.imdb, dry_run=not args.apply)
