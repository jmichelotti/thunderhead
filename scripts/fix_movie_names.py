#!/usr/bin/env python
"""
Normalize movie filenames/folders for Jellyfin using OMDb (IMDb) lookups.

- Looks at all video files in MOVIES_ROOT (not in subfolders).
- For each movie:
    * Queries OMDb by title to get Year and canonical Title.
    * Creates a folder "Title (Year)" (sanitized for Windows).
    * Moves the video + matching subtitles into that folder.
    * Renames them to "Title (Year).ext".

Safe by default: DRY RUN unless you pass --apply.
"""

import os
import re
import shutil
from pathlib import Path
from typing import Optional, Dict

import requests


# ======== CONFIG ========

DEFAULT_MOVIES_ROOT = r"C:\Temp_Media\Movies"

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}

# OMDb API configuration
OMDB_API_KEY = "591dfd18"

# We are going with OPTION B:
# Use the canonical OMDb title (then sanitize it for Windows).
USE_REMOTE_TITLE = True


# ======== HELPERS ========

IMDB_ID_RE = re.compile(r"(tt\d{7,8})", re.IGNORECASE)

def sanitize_for_windows(name: str) -> str:
    """
    Sanitize a string so it is safe as a Windows file/folder name.
    Removes or replaces invalid characters and trims trailing dots/spaces.
    """
    # Characters not allowed in Windows filenames
    invalid_chars = r'<>:"/\\|?*'
    # Replace invalid characters with a space
    trans_table = str.maketrans({ch: " " for ch in invalid_chars})
    name = name.translate(trans_table)

    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name).strip()

    # Remove trailing dots/spaces
    name = name.rstrip(" .")

    return name


def try_imdb_suggestion(imdb_id: str) -> Optional[Dict[str, str]]:
    """
    Fallback: query IMDb's suggestion API for title/year.
    Used when OMDb hasn't indexed the title yet.
    """
    url = f"https://v3.sg.media-imdb.com/suggestion/t/{imdb_id}.json"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
    except Exception as e:
        print(f"  [IMDb] Suggestion API error: {e}")
        return None

    for item in data.get("d", []):
        if item.get("id", "").lower() == imdb_id.lower():
            title = item.get("l")
            year = item.get("y")
            if title and year:
                print(f"  [IMDb] Suggestion API match: {title} ({year}) [{imdb_id}]")
                return {"title": title, "year": str(year)}

    print(f"  [IMDb] Suggestion API: no match for {imdb_id}")
    return None


def try_omdb_imdb_id(imdb_id: str) -> Optional[Dict[str, str]]:
    """
    Look up a movie by IMDb ID (i=). Falls back to IMDb suggestion API.
    """
    params = {
        "apikey": OMDB_API_KEY,
        "i": imdb_id.lower(),
        "type": "movie",
    }

    try:
        resp = requests.get("https://www.omdbapi.com/", params=params, timeout=10)
        data = resp.json()
    except Exception as e:
        print(f"  [OMDb] IMDb lookup error: {e}")
        return try_imdb_suggestion(imdb_id)

    if data.get("Response") != "True":
        print(f"  [OMDb] IMDb lookup failed: {data.get('Error')}")
        return try_imdb_suggestion(imdb_id)

    title = data.get("Title")
    year = data.get("Year")

    if not title or not year:
        print("  [OMDb] Missing Title/Year in IMDb response.")
        return try_imdb_suggestion(imdb_id)

    year = "".join(ch for ch in year if ch.isdigit())[:4]
    if len(year) != 4:
        print(f"  [OMDb] Could not parse year from IMDb: {data.get('Year')}")
        return try_imdb_suggestion(imdb_id)

    print(f"  [OMDb] IMDb match: {title} ({year}) [{imdb_id}]")
    return {"title": title, "year": year}


def try_omdb_exact(title: str) -> Optional[Dict[str, str]]:
    """
    Try an exact-title OMDb lookup (t=).
    """
    params = {
        "apikey": OMDB_API_KEY,
        "t": title,
        "type": "movie",
    }

    try:
        resp = requests.get("https://www.omdbapi.com/", params=params, timeout=10)
    except Exception as e:
        print(f"  [OMDb] Error connecting to OMDb (exact): {e}")
        return None

    if resp.status_code != 200:
        print(f"  [OMDb] HTTP {resp.status_code} from OMDb (exact).")
        return None

    try:
        data = resp.json()
    except ValueError:
        print("  [OMDb] Invalid JSON response (exact).")
        return None

    if data.get("Response") != "True":
        print(f"  [OMDb] Exact lookup failed: {data.get('Error', 'Unknown error')}")
        return None

    raw_title = data.get("Title")
    raw_year = data.get("Year")

    if not raw_title or not raw_year:
        print("  [OMDb] Missing Title/Year in exact response.")
        return None

    year = "".join(ch for ch in raw_year if ch.isdigit())[:4]
    if len(year) != 4:
        print(f"  [OMDb] Could not parse year from exact: {raw_year}")
        return None

    print(f"  [OMDb] Exact match: {raw_title} ({year})")
    return {"title": raw_title, "year": year}


def try_omdb_search(term: str) -> Optional[Dict[str, str]]:
    """
    Try a fuzzy search using s=term and pick the first result.
    """
    params = {
        "apikey": OMDB_API_KEY,
        "s": term,
        "type": "movie",
    }

    try:
        resp = requests.get("https://www.omdbapi.com/", params=params, timeout=10)
    except Exception as e:
        print(f"  [OMDb] Error connecting to OMDb (search): {e}")
        return None

    if resp.status_code != 200:
        print(f"  [OMDb] HTTP {resp.status_code} from OMDb (search).")
        return None

    try:
        data = resp.json()
    except ValueError:
        print("  [OMDb] Invalid JSON response (search).")
        return None

    if data.get("Response") != "True" or "Search" not in data:
        print(f"  [OMDb] Search failed: {data.get('Error', 'Unknown error')}")
        return None

    first = data["Search"][0]
    raw_title = first.get("Title")
    raw_year = first.get("Year")

    if not raw_title or not raw_year:
        print("  [OMDb] Missing Title/Year in search result.")
        return None

    year = "".join(ch for ch in raw_year if ch.isdigit())[:4]
    if len(year) != 4:
        print(f"  [OMDb] Could not parse year from search: {raw_year}")
        return None

    print(f"  [OMDb] Search match: {raw_title} ({year}) for term '{term}'")
    return {"title": raw_title, "year": year}


# ======== OMDb LOOKUP ========

def lookup_movie_metadata(base_title: str) -> Optional[Dict[str, str]]:
    """
    Look up movie metadata from OMDb by title, with fallbacks:

    1. Exact lookup using the full title.
    2. If that fails, split on '-' and try search (s=) on each part.
    3. As a last resort, try search on the full base title.
    """
    if not OMDB_API_KEY:
        print("  [OMDb] No API key configured; skipping lookup.")
        return None

    # 0) IMDb ID lookup (highest priority)
    m = IMDB_ID_RE.search(base_title)
    if m:
        meta = try_omdb_imdb_id(m.group(1))
        if meta:
            return meta
        # Strip the ID from the title and fall through to title-based lookups
        base_title = IMDB_ID_RE.sub("", base_title).strip()

    # 1) Exact match on full title
    meta = try_omdb_exact(base_title)
    if meta:
        return meta

    # 2) Split on '-' and try search on each side
    parts = [p.strip() for p in base_title.split('-') if p.strip()]
    for p in parts:
        meta = try_omdb_search(p)
        if meta:
            return meta

    # 3) Fallback: search using the full base title
    meta = try_omdb_search(base_title)
    if meta:
        return meta

    print("  [OMDb] All lookups failed for this title.")
    return None


def base_has_year(base_name: str) -> bool:
    """
    Rough check: does the base_name already contain (YYYY)?
    """
    if "(" in base_name and ")" in base_name:
        inner = base_name.split("(")[-1].split(")")[0].strip()
        if len(inner) == 4 and inner.isdigit():
            return True
    return False


def make_target_title(base_name: str) -> str:
    """
    Determine the target "Title (Year)" for a movie.

    - If base name already has (Year), just use it (sanitized).
    - Otherwise, do OMDb lookup with fallbacks.
    - If lookup fails, just use base_name as-is (sanitized).
    """
    if base_has_year(base_name):
        print("  [Info] Name already includes a year; keeping as-is (but sanitizing).")
        raw = base_name
        safe = sanitize_for_windows(raw)
        if raw != safe:
            print(f"  [Info] Sanitized title: {safe}")
        return safe

    meta = lookup_movie_metadata(base_name)
    if not meta:
        print("  [Info] Using original name (lookup failed).")
        raw = base_name
        safe = sanitize_for_windows(raw)
        if raw != safe:
            print(f"  [Info] Sanitized title: {safe}")
        return safe

    title = meta["title"] if USE_REMOTE_TITLE else base_name
    year = meta["year"]
    raw = f"{title} ({year})"
    safe = sanitize_for_windows(raw)
    if raw != safe:
        print(f"  [Info] Sanitized title: {safe}")
    return safe


# ======== CORE LOGIC ========

def process_movies(movies_root: Path, dry_run: bool = True) -> None:
    if not movies_root.is_dir():
        raise SystemExit(f"Movies root does not exist or is not a directory: {movies_root}")

    print(f"Movies root: {movies_root}")
    print(f"DRY RUN: {dry_run}")
    print("=" * 60)

    # Only look at files directly inside the movies root (not subfolders)
    for item in sorted(movies_root.iterdir()):
        if not item.is_file():
            continue

        ext = item.suffix.lower()
        if ext not in VIDEO_EXTS:
            continue

        base_name = item.stem  # e.g. "Star Wars Episode IV - A New Hope"

        print(f"\nFound movie: {item.name}")
        print(f"  Base name: {base_name}")

        target_title = make_target_title(base_name)
        target_dir = movies_root / target_title
        target_video = target_dir / f"{target_title}{ext}"

        print(f"  Target dir:  {target_dir}")
        print(f"  Target file: {target_video.name}")

        # Find matching subtitle files with the same base name in the root
        matching_subs = []
        for sub_item in movies_root.iterdir():
            if not sub_item.is_file():
                continue
            if sub_item.stem != base_name:
                continue
            if sub_item.suffix.lower() in SUB_EXTS:
                matching_subs.append(sub_item)

        if matching_subs:
            print("  Subtitles:")
            for s in matching_subs:
                print(f"    - {s.name}")
        else:
            print("  No subtitles found for this movie.")

        if dry_run:
            print("  [DRY RUN] Would create folder and move/rename files.")
            continue

        # Actually create directory
        target_dir.mkdir(exist_ok=True)

        # Move video
        if target_video.exists():
            print(f"  WARNING: Target video already exists, skipping move: {target_video}")
        else:
            print(f"  Moving video -> {target_video}")
            shutil.move(str(item), str(target_video))

        # Move subtitles
        for sub in matching_subs:
            sub_ext = sub.suffix
            target_sub = target_dir / f"{target_title}{sub_ext}"
            if target_sub.exists():
                print(f"  WARNING: Target subtitle already exists, skipping: {target_sub}")
                continue
            print(f"  Moving subtitle -> {target_sub}")
            shutil.move(str(sub), str(target_sub))

# ======== CLI ENTRYPOINT ========

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Normalize movie folders and filenames for Jellyfin using OMDb (IMDb) lookups."
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_MOVIES_ROOT,
        help="Root movie directory (default: %(default)s)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move/rename files. Without this, just print what would happen.",
    )

    args = parser.parse_args()
    movies_root = Path(args.root)

    process_movies(movies_root, dry_run=not args.apply)
