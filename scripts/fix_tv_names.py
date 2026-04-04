#!/usr/bin/env python
"""
Normalize TV show filenames/folders for Jellyfin using OMDb (IMDb) lookups.

Supports combined episodes ONLY when explicitly written with &:
  S06E20&21  ->  S06E20-E21
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Tuple

import requests


# ========= CONFIG =========

TV_ROOT = r"C:\Temp_Media\TV Shows"

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}

OMDB_API_KEY = "591dfd18"

# 🔒 IMDb title overrides (authoritative)
IMDB_TITLE_OVERRIDES = {
    "tt38673133": "Taylor Swift The Eras Tour - The End of an Era",
}


# ========= HELPERS =========

def sanitize_for_windows(name: str) -> str:
    invalid_chars = r'<>:"/\\|?*'
    name = name.translate(str.maketrans({ch: " " for ch in invalid_chars}))
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(" .")


def strip_year_from_show_title(show: str) -> str:
    return re.sub(r"\(\s*\d{4}\s*\)\s*$", "", show).strip()


def camel_to_spaces(name: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', " ", name).strip()


def normalize_show_key(show: str) -> str:
    base = strip_year_from_show_title(show).lower()
    return re.sub(r"[^a-z0-9]+", "", base)


def strip_sub_lang_suffix(stem: str) -> str:
    return re.sub(
        r"\.(?:[a-z]{2,3}(?:-[A-Z]{2})?)$",
        "",
        stem,
        flags=re.IGNORECASE
    )


def extract_imdb_id(text: str) -> Optional[str]:
    m = re.search(r"\b(tt\d{7,9})\b", text, re.IGNORECASE)
    return m.group(1) if m else None


def remove_imdb_id(text: str) -> str:
    return re.sub(r"\btt\d{7,9}\b", "", text, flags=re.IGNORECASE).strip()


# ========= DATA =========

@dataclass
class EpisodeInfo:
    show_raw: str
    show_key: str
    season: int
    ep_start: int
    ep_end: int
    imdb_id: Optional[str] = None
    ep_title: Optional[str] = None


# ========= EPISODE PARSING =========

EPISODE_PATTERNS = [
    re.compile(
        r"^(?P<show>.+?)\s*S(?P<season>\d+)\s*E(?P<e1>\d+)\s*&\s*(?P<e2>\d+)",
        re.IGNORECASE
    ),
    re.compile(
        r"^(?P<show>.+?)\s*S(?P<season>\d+)\s*E(?P<e1>\d+)\b",
        re.IGNORECASE
    ),
    # "Show 01 - Title" style (no season marker → defaults to Season 1)
    re.compile(
        r"^(?P<show>.+?)\s+(?P<e1>\d{1,3})\s*[-–]\s*(?P<title>.+)",
        re.IGNORECASE
    ),
]


def parse_episode_info(stem: str) -> Optional[EpisodeInfo]:
    text = stem.strip()
    imdb_id = extract_imdb_id(text)
    text = remove_imdb_id(text)

    for pattern in EPISODE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue

        show_raw = m.group("show").strip(" .-_")
        season = int(m.group("season")) if m.groupdict().get("season") else 1
        e1 = int(m.group("e1"))
        e2 = int(m.group("e2")) if m.groupdict().get("e2") else e1
        ep_title = m.group("title").strip() if m.groupdict().get("title") else None

        return EpisodeInfo(
            show_raw=show_raw,
            show_key=normalize_show_key(show_raw),
            season=season,
            ep_start=e1,
            ep_end=e2,
            imdb_id=imdb_id,
            ep_title=ep_title,
        )
    return None


# ========= OMDb HELPERS =========

SERIES_CACHE: Dict[str, Dict[str, str]] = {}


def try_omdb(params) -> Optional[Dict[str, str]]:
    try:
        r = requests.get("https://www.omdbapi.com/", params=params, timeout=10)
        data = r.json()
    except Exception:
        return None

    if data.get("Response") != "True":
        return None

    year = "".join(c for c in data.get("Year", "") if c.isdigit())[:4]
    if len(year) != 4:
        return None

    return {"title": data.get("Title"), "year": year}


def lookup_series_metadata(ep: EpisodeInfo) -> Optional[Dict[str, str]]:
    print(f"🔍 Looking up series: {ep.show_raw}", flush=True)
    if not OMDB_API_KEY:
        return None

    cache_key = ep.imdb_id or ep.show_key
    if cache_key in SERIES_CACHE:
        return SERIES_CACHE[cache_key]

    # 1️⃣ IMDb ID lookup (authoritative)
    if ep.imdb_id:
        meta = try_omdb({
            "apikey": OMDB_API_KEY,
            "i": ep.imdb_id,
            "type": "series"
        })
        if meta:
            title = IMDB_TITLE_OVERRIDES.get(ep.imdb_id, meta["title"])
            meta = {"title": title, "year": meta["year"]}
            SERIES_CACHE[cache_key] = meta
            return meta

    # 2️⃣ Title fallback
    base = strip_year_from_show_title(ep.show_raw)
    candidates = [base]

    if " " not in base:
        spaced = camel_to_spaces(base)
        if spaced != base:
            candidates.append(spaced)

    for title in candidates:
        meta = try_omdb({
            "apikey": OMDB_API_KEY,
            "t": title,
            "type": "series"
        })
        if meta:
            SERIES_CACHE[cache_key] = meta
            return meta

    return None


# ========= CORE LOGIC =========

def episode_key(ep: EpisodeInfo) -> Tuple:
    return (ep.show_key, ep.season, ep.ep_start, ep.ep_end)


def process_tv(tv_root: Path, dry_run: bool = True) -> None:
    print(f"TV root: {tv_root}")
    print(f"DRY RUN: {dry_run}")
    print("=" * 60)

    processed = set()

    # ---------- PASS 1: VIDEOS ----------
    for video in sorted(tv_root.iterdir()):
        if not video.is_file() or video.suffix.lower() not in VIDEO_EXTS:
            continue

        ep = parse_episode_info(video.stem)
        if not ep:
            print(f"Skipping (could not parse): {video.name}")
            continue

        processed.add(episode_key(ep))

        meta = lookup_series_metadata(ep)
        series_name = (
            sanitize_for_windows(f"{meta['title']} ({meta['year']})")
            if meta else
            sanitize_for_windows(strip_year_from_show_title(ep.show_raw))
        )

        season_dir = tv_root / series_name / f"Season {ep.season:02d}"

        ep_part = (
            f"E{ep.ep_start:02d}"
            if ep.ep_start == ep.ep_end
            else f"E{ep.ep_start:02d}-E{ep.ep_end:02d}"
        )

        if ep.ep_title:
            base = f"{series_name} - S{ep.season:02d}{ep_part} - {sanitize_for_windows(ep.ep_title)}"
        else:
            base = f"{series_name} S{ep.season:02d}{ep_part}"
        target_video = season_dir / f"{base}{video.suffix}"

        matching_subs = []
        for sub in video.parent.iterdir():
            if sub.suffix.lower() in SUB_EXTS:
                clean = strip_sub_lang_suffix(sub.stem)
                sub_ep = parse_episode_info(clean)
                if sub_ep and episode_key(sub_ep) == episode_key(ep):
                    matching_subs.append(sub)

        if dry_run:
            print(f"[DRY RUN] Would move {video.name} -> {target_video}")
            continue

        season_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(video, target_video)

        for sub in matching_subs:
            shutil.move(sub, season_dir / f"{base}{sub.suffix}")

    # ---------- PASS 2: SUBTITLES ONLY ----------
    print("\nScanning for subtitle-only episodes…")

    for sub in sorted(tv_root.iterdir()):
        if not sub.is_file() or sub.suffix.lower() not in SUB_EXTS:
            continue

        clean = strip_sub_lang_suffix(sub.stem)
        ep = parse_episode_info(clean)
        if not ep or episode_key(ep) in processed:
            continue

        meta = lookup_series_metadata(ep)
        series_name = (
            sanitize_for_windows(f"{meta['title']} ({meta['year']})")
            if meta else
            sanitize_for_windows(strip_year_from_show_title(ep.show_raw))
        )

        season_dir = tv_root / series_name / f"Season {ep.season:02d}"

        ep_part = (
            f"E{ep.ep_start:02d}"
            if ep.ep_start == ep.ep_end
            else f"E{ep.ep_start:02d}-E{ep.ep_end:02d}"
        )

        if ep.ep_title:
            base = f"{series_name} - S{ep.season:02d}{ep_part} - {sanitize_for_windows(ep.ep_title)}"
        else:
            base = f"{series_name} S{ep.season:02d}{ep_part}"
        target_sub = season_dir / f"{base}{sub.suffix}"

        if dry_run:
            print(f"[DRY RUN] Would move subtitle {sub.name} -> {target_sub}")
            continue

        season_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(sub, target_sub)


# ========= CLI =========

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=TV_ROOT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    process_tv(Path(args.root), dry_run=not args.apply)
