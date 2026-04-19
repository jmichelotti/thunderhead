"""Detect missing episodes for currently-airing shows users are watching."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jellyfin_client as jf
import tvmaze_client as tvmaze
from config import TIMEZONE

log = logging.getLogger("episode_gaps")

CACHE_PATH = Path(__file__).resolve().parent / "tracked_shows.json"

# TVmaze season + offset = Jellyfin season (e.g. AU Survivor 2016 reboot is -2)
_SEASON_OFFSETS: dict[str, int] = {
    "Australian Survivor": -2,
}

_IGNORE_SHOWS: set[str] = {
    "SpongeBob SquarePants",
}


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(data: dict) -> None:
    CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def _resolve_tvmaze_id(
    series: dict, cache: dict
) -> int | None:
    """Resolve a Jellyfin series to a TVmaze show ID, using cache or external lookup."""
    jf_id = series["Id"]

    if jf_id in cache and cache[jf_id].get("tvmaze_id"):
        return cache[jf_id]["tvmaze_id"]

    providers = series.get("ProviderIds", {})

    # Try TVDB first (most reliable for TV), then IMDb
    tvdb = providers.get("Tvdb")
    if tvdb:
        result = await tvmaze.lookup_by_tvdb(tvdb)
        if result:
            return result["id"]

    imdb = providers.get("Imdb")
    if imdb:
        result = await tvmaze.lookup_by_imdb(imdb)
        if result:
            return result["id"]

    # Fallback: name search
    results = await tvmaze.search_show(series.get("Name", ""))
    if results:
        return results[0]["show"]["id"]

    return None


async def _get_watched_show_names(days: int = 90) -> set[str]:
    """Get unique show names from recent playback activity."""
    raw = await jf.custom_query(
        f"""
        SELECT DISTINCT
            CASE
                WHEN ItemName LIKE '%% - %%' THEN SUBSTR(ItemName, 1, INSTR(ItemName, ' - ') - 1)
                ELSE ItemName
            END as show_name
        FROM PlaybackActivity
        WHERE ItemType = 'Episode'
          AND DateCreated >= datetime('now', '-{days} days')
        """,
        replace_user_id=False,
    )
    return {row[0] for row in raw.get("results", []) if row[0]}


async def scan_gaps(days: int = 90, recent_only: bool = True) -> list[dict]:
    """
    Scan for missing episodes in shows users are actively watching.

    If recent_only is True, only checks the latest season in Jellyfin
    (and the next one), which filters out old reunion/clip episodes
    you never intended to download.
    """
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    cache = _load_cache()

    # 1. Get shows users are watching
    watched_names = await _get_watched_show_names(days)
    if not watched_names:
        return []

    # 2. Get all series from Jellyfin with status info
    all_series = await jf.series_list()

    # Filter to: user is watching + status is Continuing
    # Collect all candidates per name (handles duplicates like two "The Boys" entries)
    candidates: dict[str, list[dict]] = {}
    for s in all_series:
        name = s.get("Name", "")
        status = s.get("Status", "")
        if name in watched_names and status == "Continuing":
            candidates.setdefault(name, []).append(s)

    log.info("Tracking %d continuing series out of %d watched", len(candidates), len(watched_names))

    # 3. For each tracked show, resolve TVmaze ID and compare episodes
    results = []
    for name, entries in candidates.items():
        if name in _IGNORE_SHOWS:
            continue
        series = entries[0]
        try:
            tvmaze_id = await _resolve_tvmaze_id(series, cache)
        except Exception as e:
            log.warning("TVmaze lookup failed for %s: %s", name, e)
            continue

        if tvmaze_id is None:
            log.warning("Could not find %s on TVmaze", name)
            continue

        # Merge episodes across all Jellyfin entries for this show name
        # (handles duplicates like two "The Boys" entries on different drives)
        jf_set: set[tuple[int, int]] = set()
        jf_seasons: set[int] = set()
        best_jf_id = entries[0]["Id"]
        best_count = 0

        for entry in entries:
            try:
                eps = await jf.series_episodes(entry["Id"])
            except Exception:
                continue
            if len(eps) > best_count:
                best_count = len(eps)
                best_jf_id = entry["Id"]
            for ep in eps:
                s_num = ep.get("ParentIndexNumber")
                e_num = ep.get("IndexNumber")
                if s_num is not None and e_num is not None:
                    jf_set.add((s_num, e_num))
                    jf_seasons.add(s_num)

        # Update cache with the best entry
        cache[best_jf_id] = {
            "name": name,
            "tvmaze_id": tvmaze_id,
            "tvdb_id": series.get("ProviderIds", {}).get("Tvdb"),
            "last_checked": today.isoformat(),
        }

        # Get TVmaze episodes
        try:
            tvmaze_episodes = await tvmaze.show_episodes(tvmaze_id)
        except Exception as e:
            log.warning("TVmaze episode fetch failed for %s: %s", name, e)
            continue

        if jf_seasons:
            latest = max(jf_seasons)
            if recent_only:
                # Only check the latest season + next expected season
                jf_seasons = {latest, latest + 1}
            else:
                # Check all seasons we have + next expected
                jf_seasons.add(latest + 1)

        offset = _SEASON_OFFSETS.get(name, 0)

        missing = []
        for ep in tvmaze_episodes:
            s_num = ep.get("season")
            e_num = ep.get("number")
            airdate = ep.get("airdate")

            if not s_num or not e_num or not airdate:
                continue
            if s_num == 0:
                continue

            jf_s = s_num + offset
            if jf_s not in jf_seasons:
                continue

            try:
                aired = date.fromisoformat(airdate)
            except ValueError:
                continue

            if aired > today:
                continue

            if (jf_s, e_num) not in jf_set:
                missing.append({
                    "season": jf_s,
                    "episode": e_num,
                    "code": f"S{jf_s:02d}E{e_num:02d}",
                    "title": ep.get("name", ""),
                    "airdate": airdate,
                })

        if missing:
            missing.sort(key=lambda x: (x["season"], x["episode"]))
            results.append({
                "show": name,
                "jellyfin_id": best_jf_id,
                "tvmaze_id": tvmaze_id,
                "total_in_jellyfin": len(jf_set),
                "missing_episodes": missing,
            })

    _save_cache(cache)
    results.sort(key=lambda x: x["show"])
    return results


