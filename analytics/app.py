"""Jellyfin analytics dashboard — always-on FastAPI service on port 1201."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import episode_gaps
import jellyfin_client as jf

log = logging.getLogger("analytics")


async def _safe(coro, fallback):
    """Run a coroutine; return fallback on any exception instead of crashing."""
    try:
        return await coro
    except Exception as e:
        log.warning("API call failed: %s", e)
        return fallback

app = FastAPI(title="Thunderhead Jellyfin Analytics")

WRAPPED_DIR = Path(__file__).resolve().parent.parent / "wrapped"
if WRAPPED_DIR.is_dir():
    app.mount("/wrapped", StaticFiles(directory=str(WRAPPED_DIR), html=True), name="wrapped")


def _ticks_to_seconds(ticks: int | None) -> float | None:
    if ticks is None:
        return None
    return round(ticks / 10_000_000, 1)


def _bytes_to_gb(b: int | None) -> float | None:
    if b is None:
        return None
    return round(b / (1024 ** 3), 2)


def _format_session(s: dict) -> dict:
    now_playing = s.get("NowPlayingItem")
    play_state = s.get("PlayState") or {}
    transcode = s.get("TranscodingInfo")

    item = None
    if now_playing:
        item = {
            "name": now_playing.get("Name"),
            "type": now_playing.get("Type"),
            "series_name": now_playing.get("SeriesName"),
            "season": now_playing.get("ParentIndexNumber"),
            "episode": now_playing.get("IndexNumber"),
            "year": now_playing.get("ProductionYear"),
            "runtime_s": _ticks_to_seconds(now_playing.get("RunTimeTicks")),
        }

    position_s = _ticks_to_seconds(play_state.get("PositionTicks"))
    runtime_s = item["runtime_s"] if item else None
    progress_pct = None
    if position_s and runtime_s and runtime_s > 0:
        progress_pct = round(position_s / runtime_s * 100, 1)

    result = {
        "user": s.get("UserName"),
        "client": s.get("Client"),
        "device": s.get("DeviceName"),
        "now_playing": item,
        "position_s": position_s,
        "progress_pct": progress_pct,
        "is_paused": play_state.get("IsPaused", False),
        "play_method": play_state.get("PlayMethod"),
        "remote_address": s.get("RemoteEndPoint"),
        "last_activity": s.get("LastActivityDate"),
    }

    if transcode:
        result["transcoding"] = {
            "video_codec": transcode.get("VideoCodec"),
            "audio_codec": transcode.get("AudioCodec"),
            "is_video_direct": transcode.get("IsVideoDirect"),
            "is_audio_direct": transcode.get("IsAudioDirect"),
            "width": transcode.get("Width"),
            "height": transcode.get("Height"),
            "completion_pct": transcode.get("CompletionPercentage"),
            "hw_accel": transcode.get("HardwareAccelerationType"),
        }

    return result


async def _server_status() -> dict:
    pong = await jf.ping()
    if pong is None:
        return {"online": False}

    info = await jf.system_info_public()
    return {
        "online": True,
        "server_name": info.get("ServerName"),
        "version": info.get("Version"),
    }


async def _active_sessions() -> dict:
    sessions = await jf.active_sessions()
    watching = [s for s in sessions if s.get("NowPlayingItem")]
    return {
        "total_sessions": len(sessions),
        "active_streams": len(watching),
        "streams": [_format_session(s) for s in watching],
    }


async def _library_stats() -> dict:
    movies, series, episodes = await asyncio.gather(
        jf.item_count("Movie"),
        jf.item_count("Series"),
        jf.item_count("Episode"),
    )

    libs = await jf.library_folders()
    libraries = []
    for lib in libs:
        libraries.append({
            "name": lib.get("Name"),
            "type": lib.get("CollectionType"),
            "paths": lib.get("Locations", []),
        })

    # Deduplicate drives across libraries and get real disk usage locally
    seen_drives: dict[str, dict] = {}
    for lib in libs:
        for path in lib.get("Locations", []):
            drive = path[:2].upper() if len(path) >= 2 and path[1] == ":" else path
            if drive not in seen_drives:
                try:
                    usage = shutil.disk_usage(drive + "\\")
                    seen_drives[drive] = {
                        "drive": drive,
                        "total_gb": _bytes_to_gb(usage.total),
                        "used_gb": _bytes_to_gb(usage.used),
                        "free_gb": _bytes_to_gb(usage.free),
                    }
                except OSError:
                    pass
    storage = list(seen_drives.values())
    if storage:
        storage.append({
            "drive": "TOTAL",
            "total_gb": round(sum(d["total_gb"] or 0 for d in storage), 2),
            "used_gb": round(sum(d["used_gb"] or 0 for d in storage), 2),
            "free_gb": round(sum(d["free_gb"] or 0 for d in storage), 2),
        })

    return {
        "movies": movies,
        "series": series,
        "episodes": episodes,
        "libraries": libraries,
        "storage": storage,
    }


async def _users_summary() -> list[dict]:
    raw = await jf.users()
    return [
        {
            "name": u.get("Name"),
            "is_admin": u.get("Policy", {}).get("IsAdministrator", False),
            "last_login": u.get("LastLoginDate"),
            "last_activity": u.get("LastActivityDate"),
        }
        for u in raw
    ]


def _safe_time(seconds: int | None) -> int | None:
    """Clamp negative values from Playback Reporting integer overflow."""
    if seconds is None:
        return None
    return max(seconds, 0)


def _seconds_to_human(s: int) -> str:
    if s <= 0:
        return "0m"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


async def _per_user_wrapped(days: int) -> list[dict]:
    """Build a per-user wrapped-style summary from raw playback events."""
    raw = await jf.custom_query(
        f"""
        SELECT UserId, ItemType, ItemName, PlayDuration, DateCreated
        FROM PlaybackActivity
        WHERE DateCreated >= datetime('now', '-{days} days')
        ORDER BY DateCreated
        """,
        replace_user_id=True,
    )

    users: dict[str, dict] = {}
    for row in raw.get("results", []):
        user, item_type, item_name, duration_str, date = row
        duration = max(int(duration_str), 0) if duration_str else 0

        if user not in users:
            users[user] = {
                "total_plays": 0,
                "total_seconds": 0,
                "movies": {},
                "shows": {},
                "first_play": date,
                "last_play": date,
            }
        u = users[user]
        u["total_plays"] += 1
        u["total_seconds"] += duration
        u["last_play"] = date

        # Extract show name from "Show - sXXeYY - Episode" format
        if item_type == "Episode":
            show = item_name.split(" - ")[0] if " - " in item_name else item_name
            u["shows"][show] = u["shows"].get(show, 0) + duration
        elif item_type == "Movie":
            u["movies"][item_name] = u["movies"].get(item_name, 0) + duration

    result = []
    for name, u in users.items():
        top_shows = sorted(u["shows"].items(), key=lambda x: x[1], reverse=True)[:5]
        top_movies = sorted(u["movies"].items(), key=lambda x: x[1], reverse=True)[:5]
        result.append({
            "user": name,
            "total_plays": u["total_plays"],
            "total_time_s": u["total_seconds"],
            "total_time_human": _seconds_to_human(u["total_seconds"]),
            "top_shows": [{"name": n, "time_s": t, "time_human": _seconds_to_human(t)} for n, t in top_shows],
            "top_movies": [{"name": n, "time_s": t, "time_human": _seconds_to_human(t)} for n, t in top_movies],
            "first_play": u["first_play"],
            "last_play": u["last_play"],
        })

    result.sort(key=lambda x: x["total_time_s"], reverse=True)
    return result


@app.get("/")
def root() -> dict:
    return {
        "ui": "/wrapped",
        "endpoints": [
            "/healthz",
            "/status",
            "/sessions",
            "/library",
            "/users",
            "/playback/activity?days=30",
            "/playback/most-watched",
            "/playback/breakdowns",
            "/playback/hourly?days=30",
            "/playback/wrapped?days=365",
            "/playback/history/{username}?days=365",
            "/playback/currently-watching?days=30",
            "/episodes/gaps?days=90",
        ]
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/status")
async def status() -> dict:
    server, sessions, library, user_list = await asyncio.gather(
        _safe(_server_status(), {"online": False}),
        _safe(_active_sessions(), {"total_sessions": 0, "active_streams": 0, "streams": []}),
        _safe(_library_stats(), {"movies": 0, "series": 0, "episodes": 0, "libraries": [], "storage": []}),
        _safe(_users_summary(), []),
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "server": server,
        "sessions": sessions,
        "library": library,
        "users": user_list,
    }


@app.get("/sessions")
async def sessions() -> dict:
    return await _active_sessions()


@app.get("/library")
async def library() -> dict:
    return await _library_stats()


@app.get("/users")
async def users_endpoint() -> list[dict]:
    return await _users_summary()


@app.get("/playback/activity")
async def playback_activity(days: int = 30) -> dict:
    raw = await jf.custom_query(
        f"""
        SELECT UserId, date(DateCreated) as d, COUNT(*) as cnt,
               SUM(MAX(PlayDuration, 0)) as secs
        FROM PlaybackActivity
        WHERE DateCreated >= datetime('now', '-{days} days')
        GROUP BY UserId, d
        ORDER BY d
        """,
        replace_user_id=True,
    )

    plays: dict[str, dict[str, int]] = {}
    seconds: dict[str, dict[str, int]] = {}
    for row in raw.get("results", []):
        user, date, count, secs = row
        if user not in plays:
            plays[user] = {}
            seconds[user] = {}
        plays[user][date] = int(count)
        seconds[user][date] = int(secs)

    return {"days": days, "plays_per_day": plays, "seconds_per_day": seconds}


@app.get("/playback/most-watched")
async def most_watched(days: int = 365) -> dict:
    # Compute from raw events in Python to avoid SQLite 32-bit integer overflow
    raw = await jf.custom_query(
        f"""
        SELECT ItemType, ItemName, PlayDuration
        FROM PlaybackActivity
        WHERE DateCreated >= datetime('now', '-{days} days')
        """,
        replace_user_id=False,
    )

    shows: dict[str, dict] = {}
    movies: dict[str, dict] = {}
    for row in raw.get("results", []):
        item_type, item_name, duration_str = row
        duration = max(int(duration_str), 0) if duration_str else 0

        if item_type == "Episode":
            show = item_name.split(" - ")[0] if " - " in item_name else item_name
            if show not in shows:
                shows[show] = {"plays": 0, "time_s": 0}
            shows[show]["plays"] += 1
            shows[show]["time_s"] += duration
        elif item_type == "Movie":
            if item_name not in movies:
                movies[item_name] = {"plays": 0, "time_s": 0}
            movies[item_name]["plays"] += 1
            movies[item_name]["time_s"] += duration

    def _fmt(bucket: dict[str, dict]) -> list[dict]:
        return [
            {
                "name": name,
                "plays": v["plays"],
                "time_s": v["time_s"],
                "time_human": _seconds_to_human(v["time_s"]),
            }
            for name, v in sorted(bucket.items(), key=lambda x: x[1]["time_s"], reverse=True)
        ]

    return {"days": days, "tv_shows": _fmt(shows), "movies": _fmt(movies)}


@app.get("/playback/breakdowns")
async def breakdowns(days: int = 365) -> dict:
    # Compute from raw events in Python to avoid SQLite 32-bit integer overflow
    raw = await jf.custom_query(
        f"""
        SELECT UserId, PlaybackMethod, ClientName, DeviceName, PlayDuration
        FROM PlaybackActivity
        WHERE DateCreated >= datetime('now', '-{days} days')
        """,
        replace_user_id=True,
    )

    buckets: dict[str, dict[str, dict]] = {
        "by_user": {}, "by_playback_method": {},
        "by_client": {}, "by_device": {},
    }
    field_map = [
        ("by_user", 0), ("by_playback_method", 1),
        ("by_client", 2), ("by_device", 3),
    ]
    for row in raw.get("results", []):
        duration = max(int(row[4]), 0) if row[4] else 0
        for bucket_key, col_idx in field_map:
            label = row[col_idx] or "Unknown"
            b = buckets[bucket_key]
            if label not in b:
                b[label] = {"plays": 0, "time_s": 0}
            b[label]["plays"] += 1
            b[label]["time_s"] += duration

    def _fmt(bucket: dict[str, dict]) -> list[dict]:
        return [
            {
                "label": label,
                "plays": v["plays"],
                "time_s": v["time_s"],
                "time_human": _seconds_to_human(v["time_s"]),
            }
            for label, v in sorted(bucket.items(), key=lambda x: x[1]["time_s"], reverse=True)
        ]

    return {
        "days": days,
        "by_user": _fmt(buckets["by_user"]),
        "by_playback_method": _fmt(buckets["by_playback_method"]),
        "by_client": _fmt(buckets["by_client"]),
        "by_device": _fmt(buckets["by_device"]),
    }


@app.get("/playback/hourly")
async def hourly(days: int = 365) -> dict:
    # Plugin's HourlyReport endpoint returns all zeros — build from raw SQL
    raw = await jf.custom_query(
        f"""
        SELECT strftime('%w', DateCreated) as day,
               strftime('%H', DateCreated) as hour,
               COUNT(*) as cnt
        FROM PlaybackActivity
        WHERE DateCreated >= datetime('now', '-{days} days')
        GROUP BY day, hour
        ORDER BY day, hour
        """,
        replace_user_id=False,
    )
    day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    heatmap: dict[str, dict[str, int]] = {}
    for row in raw.get("results", []):
        day_idx, hour, count = row
        day = day_names[int(day_idx)]
        if day not in heatmap:
            heatmap[day] = {}
        heatmap[day][f"{int(hour):02d}:00"] = int(count)
    return {"days": days, "heatmap": heatmap}


@app.get("/playback/wrapped")
async def wrapped(days: int = 365) -> dict:
    user_summaries = await _per_user_wrapped(days)

    total_plays = sum(u["total_plays"] for u in user_summaries)
    total_seconds = sum(u["total_time_s"] for u in user_summaries)

    # Show the actual date range of data, not just the lookback window
    earliest = min((u["first_play"] for u in user_summaries), default=None)
    latest = max((u["last_play"] for u in user_summaries), default=None)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_range": {
            "earliest": earliest,
            "latest": latest,
            "note": "Playback Reporting plugin was enabled on the earliest date — data before that was not captured",
        },
        "totals": {
            "plays": total_plays,
            "time_s": total_seconds,
            "time_human": _seconds_to_human(total_seconds),
        },
        "users": user_summaries,
    }


@app.get("/playback/history/{username}")
async def user_history(username: str, days: int = 365) -> dict:
    raw = await jf.custom_query(
        f"""
        SELECT ItemType, ItemName, PlaybackMethod, ClientName,
               DeviceName, PlayDuration, DateCreated
        FROM PlaybackActivity
        WHERE UserId = '{username}'
          AND DateCreated >= datetime('now', '-{days} days')
        ORDER BY DateCreated DESC
        """,
        replace_user_id=True,
    )

    events = []
    for row in raw.get("results", []):
        item_type, item_name, method, client, device, dur_str, date = row
        duration = max(int(dur_str), 0) if dur_str else 0
        events.append({
            "type": item_type,
            "name": item_name,
            "method": method,
            "client": client,
            "device": device,
            "duration_s": duration,
            "duration_human": _seconds_to_human(duration),
            "date": date,
        })

    return {"user": username, "days": days, "total_events": len(events), "events": events}


@app.get("/playback/currently-watching")
async def currently_watching(days: int = 30) -> dict:
    raw = await jf.custom_query(
        f"""
        SELECT UserId, ItemName, PlayDuration, DateCreated
        FROM PlaybackActivity
        WHERE ItemType = 'Episode'
          AND DateCreated >= datetime('now', '-{days} days')
        ORDER BY DateCreated DESC
        """,
        replace_user_id=True,
    )

    # Group by user → show → episodes
    user_shows: dict[str, dict[str, dict]] = {}
    for row in raw.get("results", []):
        user, item_name, dur_str, date = row
        duration = max(int(dur_str), 0) if dur_str else 0
        parts = item_name.split(" - ", 2)
        show = parts[0]
        episode_tag = parts[1] if len(parts) > 1 else ""
        episode_title = parts[2] if len(parts) > 2 else ""

        ep_label = f"{episode_tag} - {episode_title}".strip(" -") if episode_tag else item_name

        if user not in user_shows:
            user_shows[user] = {}
        if show not in user_shows[user]:
            user_shows[user][show] = {
                "episodes_watched": 0,
                "total_time_s": 0,
                "last_watched": date,
                "last_episode": ep_label,
                "seen_episodes": set(),
            }
        s = user_shows[user][show]
        ep_key = episode_tag or item_name
        if ep_key not in s["seen_episodes"]:
            s["seen_episodes"].add(ep_key)
            s["episodes_watched"] += 1
        s["total_time_s"] += duration
        if date > s["last_watched"]:
            s["last_watched"] = date
            s["last_episode"] = ep_label

    result = []
    for user, shows in user_shows.items():
        user_entry = {"user": user, "shows": []}
        for show_name, info in sorted(shows.items(), key=lambda x: x[1]["last_watched"], reverse=True):
            user_entry["shows"].append({
                "show": show_name,
                "episodes_watched": info["episodes_watched"],
                "total_time_s": info["total_time_s"],
                "total_time_human": _seconds_to_human(info["total_time_s"]),
                "last_watched": info["last_watched"],
                "last_episode": info["last_episode"],
            })
        result.append(user_entry)

    # Sort users by most recently active
    result.sort(key=lambda x: x["shows"][0]["last_watched"] if x["shows"] else "", reverse=True)
    return {"days": days, "users": result}


@app.get("/episodes/gaps")
async def episodes_gaps(days: int = 90, all_seasons: bool = False) -> dict:
    gaps = await episode_gaps.scan_gaps(days, recent_only=not all_seasons)
    total_missing = sum(len(g["missing_episodes"]) for g in gaps)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "shows_with_gaps": len(gaps),
        "total_missing_episodes": total_missing,
        "gaps": gaps,
    }


if __name__ == "__main__":
    import uvicorn
    from config import HOST, PORT

    uvicorn.run("app:app", host=HOST, port=PORT)
