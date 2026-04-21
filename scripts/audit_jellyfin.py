#!/usr/bin/env python
"""
audit_jellyfin.py - Audit Jellyfin media libraries for corruption and layout issues.

READ-ONLY with respect to media files. This script never creates, modifies,
renames, or deletes any file under the TV or Movie roots. All writes are confined
to scripts/audit_reports/ (CSV report, summary, decode cache, lock file).

Tiers:
  1 (fast, default)  - ffprobe structural checks (streams, duration, codecs, encoder tag, container)
  2 (fast, default)  - naming/layout checks against canonical Show (Year)/Season NN/ and Title (Year)/ conventions
  3 (slow, --deep)   - full ffmpeg decode sweep, cache-gated on (size, mtime)

Run:
  python audit_jellyfin.py                  # tier 1+2 across all drives
  python audit_jellyfin.py --deep           # + tier 3 decode sweep
  python audit_jellyfin.py --drive D        # limit to one or more drives
  python audit_jellyfin.py --limit 50       # cap files per root (testing)
  python audit_jellyfin.py --cpu-limit 25   # cap tier-3 ffmpeg to 25% total CPU (Win8.1+)
  python audit_jellyfin.py --no-limit       # tier-3 at full CPU, auto-threads (max throughput)
  python audit_jellyfin.py --clear-cache    # wipe the deep-decode cache

Outputs land in scripts/audit_reports/:
  YYYY-MM-DD_HHMM.csv    per-run report
  latest.csv             copy of most recent report (stable path for scheduler)
  latest_summary.txt     human-readable counts
  .deep_cache.json       tier-3 cache (never commit)
  .audit.lock            PID lock (duplicate-instance guard)
"""

from pathlib import Path
from datetime import datetime
from collections import Counter
import argparse
import csv
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time

# ============================================================
# CONFIG
# ============================================================

TV_ROOTS = [
    r"D:\TV Shows",
    r"F:\TV Shows",
    r"L:\TV Shows",
]

MOVIE_ROOTS = [
    r"D:\Movies",
    r"F:\Movies",
    r"L:\Movies",
]

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov"}
SUB_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".idx"}

# Show folders exempt from tier 2 layout checks (no year, intentional)
LAYOUT_WHITELIST = {"P90X"}

ENCODER_BAD_PATTERNS = ["hls.js", "dailymotion"]

# Jellyfin-friendly codecs (flagged as warn if not in set)
OK_VIDEO_CODECS = {"h264", "hevc", "av1", "vp9", "vp8", "mpeg4", "mpeg2video"}
OK_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "opus", "flac", "dts", "truehd", "pcm_s16le"}

# ffprobe format_name tokens expected for each extension
EXT_TO_FORMAT = {
    ".mp4": {"mp4", "mov"},
    ".mkv": {"matroska", "webm"},
    ".avi": {"avi"},
    ".mov": {"mov", "mp4"},
}

TV_EP_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
YEAR_FOLDER_RE = re.compile(r".+ \(\d{4}\)$")
SEASON_FOLDER_RE = re.compile(r"^Season \d{2}$")
MIGRATED_RE = re.compile(r"\(migrated\s*\d*\)", re.IGNORECASE)
LANG_SUFFIX_RE = re.compile(r"\.[a-z]{2,3}$", re.IGNORECASE)

SANE_MAX_DURATION_SEC = 12 * 3600

REPORTS_DIR = Path(__file__).parent / "audit_reports"
CACHE_FILE = REPORTS_DIR / ".deep_cache.json"
LOCK_FILE = REPORTS_DIR / ".audit.lock"

FIELDNAMES = ["drive", "path", "tier", "severity", "issue", "detail", "mtime"]

# ============================================================


def make_issue(path, drive, tier, severity, issue, detail="", mtime=""):
    return {
        "drive": drive,
        "path": str(path),
        "tier": tier,
        "severity": severity,
        "issue": issue,
        "detail": detail,
        "mtime": mtime,
    }


# ============================================================
# ffprobe
# ============================================================


def run_ffprobe(path: Path):
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()
            return None, (tail[-1] if tail else f"ffprobe rc={result.returncode}")
        return json.loads(result.stdout), None
    except subprocess.TimeoutExpired:
        return None, "ffprobe timeout"
    except json.JSONDecodeError as e:
        return None, f"ffprobe invalid json: {e}"
    except FileNotFoundError:
        print("[FATAL] ffprobe not found on PATH", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        return None, f"ffprobe error: {e}"


# ============================================================
# Tier 1 - structural
# ============================================================


def check_tier1(path: Path, drive: str, mtime_str: str):
    issues = []
    info, err = run_ffprobe(path)
    if info is None:
        issues.append(make_issue(path, drive, "tier1", "error", "unreadable", err or "", mtime_str))
        return issues

    streams = info.get("streams", []) or []
    fmt = info.get("format", {}) or {}

    v_streams = [s for s in streams if s.get("codec_type") == "video"]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if not v_streams:
        issues.append(make_issue(path, drive, "tier1", "error", "no_video_stream", "", mtime_str))
    if not a_streams:
        issues.append(make_issue(path, drive, "tier1", "warn", "no_audio_stream", "", mtime_str))

    try:
        duration = float(fmt.get("duration") or 0)
    except (ValueError, TypeError):
        duration = 0
    if duration <= 0:
        issues.append(make_issue(path, drive, "tier1", "error", "zero_duration", "", mtime_str))
    elif duration > SANE_MAX_DURATION_SEC:
        issues.append(make_issue(path, drive, "tier1", "warn", "suspicious_duration",
                                 f"{duration:.0f}s", mtime_str))

    def _enc_bad(enc):
        enc = (enc or "").lower()
        if enc.startswith("lavf"):
            return None
        for p in ENCODER_BAD_PATTERNS:
            if p in enc:
                return p
        return None

    fmt_tags = fmt.get("tags", {}) or {}
    if _enc_bad(fmt_tags.get("encoder")):
        issues.append(make_issue(path, drive, "tier1", "warn", "bad_encoder_tag",
                                 f"format.encoder={fmt_tags.get('encoder')}", mtime_str))
    else:
        for s in streams:
            st = s.get("tags", {}) or {}
            if _enc_bad(st.get("encoder")):
                issues.append(make_issue(path, drive, "tier1", "warn", "bad_encoder_tag",
                                         f"stream.encoder={st.get('encoder')}", mtime_str))
                break

    if v_streams:
        vcodec = (v_streams[0].get("codec_name") or "").lower()
        if vcodec and vcodec not in OK_VIDEO_CODECS:
            issues.append(make_issue(path, drive, "tier1", "warn", "unusual_video_codec",
                                     vcodec, mtime_str))
    if a_streams:
        acodec = (a_streams[0].get("codec_name") or "").lower()
        if acodec and acodec not in OK_AUDIO_CODECS:
            issues.append(make_issue(path, drive, "tier1", "warn", "unusual_audio_codec",
                                     acodec, mtime_str))

    fmt_name = (fmt.get("format_name") or "").lower()
    ext = path.suffix.lower()
    expected = EXT_TO_FORMAT.get(ext, set())
    if expected and fmt_name and not any(e in fmt_name for e in expected):
        issues.append(make_issue(path, drive, "tier1", "warn", "container_mismatch",
                                 f"ext={ext} format={fmt_name}", mtime_str))

    return issues


# ============================================================
# Tier 2 - naming/layout
# ============================================================


def check_tier2_tv(path: Path, drive: str, mtime_str: str, tv_root: Path):
    issues = []
    try:
        rel = path.relative_to(tv_root)
    except ValueError:
        return issues

    parts = rel.parts
    if len(parts) != 3:
        issues.append(make_issue(path, drive, "tier2", "warn", "bad_tv_layout",
                                 f"expected 3 path parts below root, got {len(parts)}: {rel}",
                                 mtime_str))
    else:
        show_folder, season_folder, filename = parts
        if show_folder in LAYOUT_WHITELIST:
            return issues
        if not YEAR_FOLDER_RE.match(show_folder):
            issues.append(make_issue(path, drive, "tier2", "warn", "bad_tv_layout",
                                     f"show folder missing (Year): {show_folder}", mtime_str))
        if not SEASON_FOLDER_RE.match(season_folder):
            issues.append(make_issue(path, drive, "tier2", "warn", "bad_tv_layout",
                                     f"season folder not 'Season NN': {season_folder}", mtime_str))
        if not TV_EP_RE.search(filename):
            issues.append(make_issue(path, drive, "tier2", "warn", "bad_tv_layout",
                                     f"filename missing SxxExx: {filename}", mtime_str))

    if MIGRATED_RE.search(path.name):
        issues.append(make_issue(path, drive, "tier2", "warn", "migrated_leftover",
                                 path.name, mtime_str))
    return issues


def check_tier2_movie(path: Path, drive: str, mtime_str: str, movie_root: Path):
    issues = []
    try:
        rel = path.relative_to(movie_root)
    except ValueError:
        return issues

    parts = rel.parts
    if len(parts) != 2:
        issues.append(make_issue(path, drive, "tier2", "warn", "bad_movie_layout",
                                 f"expected 2 path parts below root, got {len(parts)}: {rel}",
                                 mtime_str))
    else:
        folder, filename = parts
        if not YEAR_FOLDER_RE.match(folder):
            issues.append(make_issue(path, drive, "tier2", "warn", "bad_movie_layout",
                                     f"movie folder missing (Year): {folder}", mtime_str))
        stem = Path(filename).stem
        if stem != folder:
            issues.append(make_issue(path, drive, "tier2", "info", "movie_filename_mismatch",
                                     f"file stem '{stem}' != folder '{folder}'", mtime_str))

    if MIGRATED_RE.search(path.name):
        issues.append(make_issue(path, drive, "tier2", "warn", "migrated_leftover",
                                 path.name, mtime_str))
    return issues


def scan_orphans_and_empty_dirs(root: Path, drive: str):
    issues = []
    if not root.exists():
        return issues
    for d in root.rglob("*"):
        if not d.is_dir():
            continue
        try:
            children = list(d.iterdir())
        except OSError as e:
            # WinError 433 ("device which does not exist") and other transient
            # filesystem hiccups shouldn't kill a multi-hour run. Log and skip.
            print(f"[warn] iterdir failed on {d}: {e}", flush=True)
            continue
        if not children:
            issues.append(make_issue(d, drive, "tier2", "info", "empty_dir", "", ""))
            continue
        video_stems = {c.stem for c in children if c.is_file() and c.suffix.lower() in VIDEO_EXTS}
        for c in children:
            if c.is_file() and c.suffix.lower() in SUB_EXTS:
                stem = c.stem
                stem_no_lang = LANG_SUFFIX_RE.sub("", stem)
                if stem not in video_stems and stem_no_lang not in video_stems:
                    issues.append(make_issue(c, drive, "tier2", "info", "orphan_subtitle", "", ""))
    return issues


# ============================================================
# Windows Job Object - CPU rate control
# ============================================================
# Caps tier-3 ffmpeg CPU usage to a configurable percent of total system CPU.
# Unlike "-threads 1" (which pins one core hot), a Job Object hard-cap lets
# ffmpeg spread its work across all cores at a low duty cycle per core, so
# no single core stays hot enough to trigger sustained fan spin.
# Requires Windows 8.1+ for the hard-cap flag. Read-only: only throttles CPU.

_JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS = 15
_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_CREATE_NO_WINDOW = 0x08000000


class _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ControlFlags", ctypes.c_uint32),
        ("CpuRate", ctypes.c_uint32),
    ]


def _create_cpu_rate_job(cpu_percent: int):
    if sys.platform != "win32":
        return None
    k32 = ctypes.windll.kernel32
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError()
    info = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
    info.ControlFlags = (
        _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
    )
    info.CpuRate = int(cpu_percent) * 100  # units are 1/100 of a percent
    ok = k32.SetInformationJobObject(
        job,
        _JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        k32.CloseHandle(job)
        raise ctypes.WinError()
    return job


def _assign_pid_to_job(job, pid: int) -> bool:
    if sys.platform != "win32" or not job:
        return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(k32.AssignProcessToJobObject(job, h))
    finally:
        k32.CloseHandle(h)


def _close_job(job):
    if sys.platform != "win32" or not job:
        return
    ctypes.windll.kernel32.CloseHandle(job)


# ============================================================
# Tier 3 - decode sweep (cache-gated)
# ============================================================


def load_cache():
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    data = json.dumps(cache, indent=2)
    try:
        tmp.write_text(data, encoding="utf-8")
        readback = json.loads(tmp.read_text(encoding="utf-8"))
        if len(readback) < len(cache):
            print(f"[WARN] cache write verification failed: wrote {len(cache)} entries, "
                  f"read back {len(readback)}. Keeping old cache.", flush=True)
            tmp.unlink(missing_ok=True)
            return
        for attempt in range(5):
            try:
                os.replace(str(tmp), str(CACHE_FILE))
                return
            except OSError:
                if attempt < 4:
                    time.sleep(0.2 * (attempt + 1))
        print(f"[ERROR] save_cache: os.replace failed after 5 retries", flush=True)
        tmp.unlink(missing_ok=True)
    except Exception as e:
        print(f"[ERROR] save_cache failed: {e}", flush=True)
        tmp.unlink(missing_ok=True)


# The only shape of ffmpeg output we permit: null muxer writing to stdout.
# This guarantees ffmpeg cannot write to any media file. Enforced at runtime.
_FFMPEG_SAFE_OUTPUT_TAIL = ("-f", "null", "-")
_FFMPEG_UNSAFE_FLAGS = {"-y", "-n"}  # overwrite/no-overwrite only matter if writing


def _assert_readonly_ffmpeg_cmd(cmd):
    if tuple(cmd[-3:]) != _FFMPEG_SAFE_OUTPUT_TAIL:
        raise RuntimeError(
            f"tier3 ffmpeg cmd must end with {_FFMPEG_SAFE_OUTPUT_TAIL}; got tail {cmd[-3:]}"
        )
    for flag in _FFMPEG_UNSAFE_FLAGS:
        if flag in cmd:
            raise RuntimeError(f"tier3 ffmpeg cmd contains write-signalling flag {flag!r}: {cmd}")


def _run_ffmpeg(cmd, timeout, cpu_limit):
    """Run ffmpeg with stderr captured. If cpu_limit is set (Windows only),
    run under a Job Object with a hard CPU rate cap.
    Returns (returncode, stderr_text). Raises TimeoutExpired on timeout."""
    creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0

    if cpu_limit <= 0 or cpu_limit >= 100 or sys.platform != "win32":
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
        return result.returncode, result.stderr

    job = _create_cpu_rate_job(cpu_limit)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=creationflags,
        )
        # Assign to job immediately. The race window before assignment is at
        # most a few ms of CPU, which is negligible against multi-minute decodes.
        _assign_pid_to_job(job, proc.pid)
        try:
            _, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stderr
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    finally:
        _close_job(job)


def check_tier3(path: Path, drive: str, mtime_str: str, cache: dict, cpu_limit: int = 0):
    key = str(path)
    try:
        st = path.stat()
    except OSError as e:
        return [make_issue(path, drive, "tier3", "error", "stat_failed", str(e), mtime_str)]

    cached = cache.get(key)
    if cached and cached.get("size") == st.st_size and cached.get("mtime") == st.st_mtime:
        if cached.get("result") == "ok":
            return []
        return [make_issue(path, drive, "tier3", "error", "decode_error",
                           f"{cached.get('detail', '')} (cached)", mtime_str)]

    size_mb = st.st_size / (1024 * 1024)
    print(f"[tier3] Decoding: {path.name} ({size_mb:.0f} MB)", flush=True)

    # Build command. With CPU rate control we let ffmpeg auto-thread so work
    # spreads across cores at low duty cycle per core (keeps the fan quiet).
    # Without rate control we pin -threads 1 to avoid saturating the machine.
    cmd = ["ffmpeg", "-v", "error", "-xerror"]
    if cpu_limit <= 0:
        cmd += ["-threads", "1"]
    cmd += [
        "-nostdin", "-hide_banner",
        "-i", str(path),
        "-map", "0:v:0?", "-map", "0:a?",
        "-f", "null", "-",
    ]
    _assert_readonly_ffmpeg_cmd(cmd)

    pre_size, pre_mtime = st.st_size, st.st_mtime

    issues = []
    try:
        rc, stderr = _run_ffmpeg(cmd, timeout=3 * 3600, cpu_limit=cpu_limit)
        stderr = (stderr or "").strip()
        if rc != 0 or stderr:
            tail = stderr.splitlines()[-1] if stderr else f"rc={rc}"
            detail = tail[:500]
            issues.append(make_issue(path, drive, "tier3", "error", "decode_error", detail, mtime_str))
            cache[key] = {
                "size": st.st_size, "mtime": st.st_mtime,
                "last_checked": datetime.now().isoformat(timespec="seconds"),
                "result": "error", "detail": detail,
            }
        else:
            cache[key] = {
                "size": st.st_size, "mtime": st.st_mtime,
                "last_checked": datetime.now().isoformat(timespec="seconds"),
                "result": "ok", "detail": "",
            }
    except subprocess.TimeoutExpired:
        issues.append(make_issue(path, drive, "tier3", "error", "decode_timeout", "", mtime_str))
        return issues
    except FileNotFoundError:
        print("[FATAL] ffmpeg not found on PATH", file=sys.stderr)
        sys.exit(2)

    # Integrity check: ffmpeg with "-f null -" must never touch the input.
    # If size or mtime changed, flag it — could be a bug here or a concurrent
    # writer (Jellyfin scan, etc.). Either way the user wants to know.
    try:
        post = path.stat()
        if post.st_size != pre_size or post.st_mtime != pre_mtime:
            issues.append(make_issue(
                path, drive, "tier3", "warn", "file_modified_during_decode",
                f"size {pre_size}->{post.st_size}, mtime {pre_mtime}->{post.st_mtime}",
                mtime_str,
            ))
    except OSError:
        pass

    return issues


# ============================================================
# Walk
# ============================================================


def walk_videos(root: Path):
    if not root.exists():
        return
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            yield p


# ============================================================
# Duplicate-instance guard (PID lockfile)
# ============================================================


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        exit_code = ctypes.c_uint32(0)
        if not k32.GetExitCodeProcess(h, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def acquire_lock() -> bool:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            other_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
        except (ValueError, OSError):
            other_pid = 0
        if other_pid and _is_process_alive(other_pid):
            print(
                f"[ABORT] Another audit is already running (pid {other_pid}). "
                f"Lock: {LOCK_FILE}",
                file=sys.stderr,
            )
            return False
        print(f"[lock] Removing stale lock (pid {other_pid} not alive): {LOCK_FILE}")
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock():
    try:
        if LOCK_FILE.exists():
            pid_in_file = LOCK_FILE.read_text(encoding="utf-8").strip()
            if pid_in_file == str(os.getpid()):
                LOCK_FILE.unlink()
    except OSError:
        pass


# ============================================================
# Main
# ============================================================


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Audit Jellyfin media libraries.")
    ap.add_argument("--deep", action="store_true",
                    help="Run tier 3 full decode sweep (slow, cache-gated).")
    ap.add_argument("--drive", action="append", default=[],
                    help="Limit to specific drives (e.g. --drive D --drive F). Repeatable.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N videos per root (testing).")
    ap.add_argument("--clear-cache", action="store_true",
                    help="Clear deep-decode cache and exit.")
    ap.add_argument("--cpu-limit", type=int, default=0,
                    help="Cap tier-3 ffmpeg to N%% total CPU via Windows Job Object "
                         "(Win8.1+). 0 = no cap (use -threads 1 instead). Recommended: 25.")
    ap.add_argument("--no-limit", action="store_true",
                    help="Run tier-3 ffmpeg with no CPU cap and auto-threads "
                         "(max throughput, loud fan). Mutually exclusive with --cpu-limit.")
    args = ap.parse_args()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.clear_cache:
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
            print(f"Cleared cache: {CACHE_FILE}")
        else:
            print("No cache to clear.")
        return

    if args.no_limit and args.cpu_limit != 0:
        print("[FATAL] --no-limit cannot be combined with --cpu-limit", file=sys.stderr)
        sys.exit(2)
    if args.no_limit:
        args.cpu_limit = 100
    elif args.cpu_limit < 0 or args.cpu_limit >= 100:
        print(f"[FATAL] --cpu-limit must be 0..99, got {args.cpu_limit}", file=sys.stderr)
        sys.exit(2)
    if 0 < args.cpu_limit < 100 and sys.platform != "win32":
        print("[WARN] --cpu-limit is Windows-only; ignoring.", file=sys.stderr)
        args.cpu_limit = 0

    if not acquire_lock():
        sys.exit(3)

    # _run_audit sets this to its cache dict so signal handlers can save it
    _active_cache = [None]

    def _cleanup_and_exit(signum, frame):
        if _active_cache[0] is not None:
            print(f"\n[signal] Saving cache ({len(_active_cache[0])} entries) before exit...",
                  flush=True)
            save_cache(_active_cache[0])
        release_lock()
        sys.exit(130)

    try:
        signal.signal(signal.SIGINT, _cleanup_and_exit)
    except (ValueError, AttributeError):
        pass
    try:
        signal.signal(signal.SIGTERM, _cleanup_and_exit)
    except (ValueError, AttributeError):
        pass

    try:
        _run_audit(args, _active_cache)
    finally:
        if _active_cache[0] is not None:
            save_cache(_active_cache[0])
        release_lock()


def _run_audit(args, _active_cache=None):
    drive_filter = {d.upper().rstrip(":") for d in args.drive} if args.drive else None

    def _filter(root_list):
        out = []
        for r in root_list:
            drive = r[0].upper()
            if drive_filter is None or drive in drive_filter:
                out.append((Path(r), drive))
        return out

    tv_roots = _filter(TV_ROOTS)
    movie_roots = _filter(MOVIE_ROOTS)

    print("=== Jellyfin Audit ===")
    print(f"Started:      {datetime.now().isoformat(timespec='seconds')}")
    print(f"PID:          {os.getpid()}")
    print(f"Deep mode:    {args.deep}")
    if args.deep:
        if args.cpu_limit >= 100:
            print(f"CPU cap:      none (--no-limit, auto-threads)")
        elif args.cpu_limit > 0:
            print(f"CPU cap:      {args.cpu_limit}% (Job Object, threads auto)")
        else:
            print(f"CPU cap:      none (-threads 1)")
    print(f"TV roots:     {[str(r) for r, _ in tv_roots]}")
    print(f"Movie roots:  {[str(r) for r, _ in movie_roots]}")
    print("======================\n")

    all_issues = []
    cache = load_cache() if args.deep else {}
    if _active_cache is not None:
        _active_cache[0] = cache
    files_scanned = 0
    deep_since_save = 0
    start_time = datetime.now()

    # Count total files up front for progress reporting
    total_files = 0
    all_roots = [(r, d, "tv") for r, d in tv_roots] + [(r, d, "movie") for r, d in movie_roots]
    for root, _, _ in all_roots:
        if root.exists():
            total_files += sum(1 for _ in walk_videos(root))
    print(f"Total video files: {total_files}", flush=True)

    def _elapsed():
        delta = datetime.now() - start_time
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _process(video: Path, drive: str, kind: str, root: Path):
        nonlocal files_scanned, deep_since_save
        files_scanned += 1
        try:
            st = video.stat()
            mtime_str = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
        except OSError:
            mtime_str = ""

        all_issues.extend(check_tier1(video, drive, mtime_str))
        if kind == "tv":
            all_issues.extend(check_tier2_tv(video, drive, mtime_str, root))
        else:
            all_issues.extend(check_tier2_movie(video, drive, mtime_str, root))
        if args.deep:
            all_issues.extend(check_tier3(video, drive, mtime_str, cache, args.cpu_limit))
            deep_since_save += 1
            if deep_since_save >= 1:
                save_cache(cache)
                deep_since_save = 0

        if files_scanned % 50 == 0:
            pct = files_scanned / total_files * 100 if total_files else 0
            print(f"[progress] {files_scanned} / {total_files} ({pct:.1f}%) — {len(all_issues)} issues — elapsed {_elapsed()}", flush=True)

    for root, drive in tv_roots:
        if not root.exists():
            print(f"[WARN] Missing root: {root}")
            continue
        print(f"[scan] TV     {root}")
        count = 0
        for video in walk_videos(root):
            _process(video, drive, "tv", root)
            count += 1
            if args.limit and count >= args.limit:
                break
        all_issues.extend(scan_orphans_and_empty_dirs(root, drive))

    for root, drive in movie_roots:
        if not root.exists():
            print(f"[WARN] Missing root: {root}")
            continue
        print(f"[scan] Movies {root}")
        count = 0
        for video in walk_videos(root):
            _process(video, drive, "movie", root)
            count += 1
            if args.limit and count >= args.limit:
                break
        all_issues.extend(scan_orphans_and_empty_dirs(root, drive))

    if args.deep:
        save_cache(cache)

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    report_path = REPORTS_DIR / f"{ts}.csv"
    latest_path = REPORTS_DIR / "latest.csv"
    summary_path = REPORTS_DIR / "latest_summary.txt"

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for row in all_issues:
            w.writerow(row)

    latest_path.write_bytes(report_path.read_bytes())

    sev_counts = Counter(i["severity"] for i in all_issues)
    issue_counts = Counter(i["issue"] for i in all_issues)
    drives_label = ",".join(sorted(drive_filter)) if drive_filter else "D,F,L"
    summary_lines = [
        f"Audit run:     {datetime.now().isoformat(timespec='seconds')}",
        f"Mode:          {'tier1+tier2+tier3 (deep)' if args.deep else 'tier1+tier2'}",
        f"Drives:        {drives_label}",
        f"Files scanned: {files_scanned}",
        "",
        f"Errors:        {sev_counts.get('error', 0)}",
        f"Warnings:      {sev_counts.get('warn', 0)}",
        f"Info:          {sev_counts.get('info', 0)}",
        "",
        "Top issues:",
    ]
    for issue, n in issue_counts.most_common(15):
        summary_lines.append(f"  {n:5d} x {issue}")
    summary = "\n".join(summary_lines) + "\n"
    summary_path.write_text(summary, encoding="utf-8")

    print()
    print(summary)
    print(f"Report:  {report_path}")
    print(f"Latest:  {latest_path}")
    print(f"Summary: {summary_path}")

    if args.deep:
        md_path = _generate_issues_md(cache, files_scanned)
        print(f"Issues:  {md_path}")


def _classify_error(detail):
    if "Error opening input files" in detail:
        return "container_unreadable"
    if "/aac" in detail and "Invalid data" in detail:
        return "aac_corrupt"
    if "/h264" in detail and "Invalid data" in detail:
        return "h264_corrupt"
    if "non monotonically increasing dts" in detail:
        return "bad_timestamps"
    if "error while decoding MB" in detail:
        return "macroblock_error"
    if "Invalid data" in detail:
        return "invalid_data"
    if "File ended prematurely" in detail:
        return "truncated"
    if "Nothing was written" in detail:
        return "empty_decode"
    if "Last message repeated" in detail:
        return "repeated_errors"
    return "other"


_ERR_DESCRIPTIONS = {
    "container_unreadable": "File cannot be opened - completely broken, must redownload",
    "h264_corrupt": "Video stream corruption - visual glitches/artifacts",
    "aac_corrupt": "Audio stream corruption - audio pops/drops (often minor)",
    "bad_timestamps": "Timestamp errors - may cause seeking issues (usually fine)",
    "macroblock_error": "Video block decode failure - visible frame corruption",
    "invalid_data": "Generic stream corruption - file partially broken",
    "truncated": "File cut off mid-download - incomplete",
    "empty_decode": "No decodable content - file is effectively empty",
    "repeated_errors": "Flood of decode errors - severely corrupted",
}

_PRIORITY = {
    "container_unreadable": 1,
    "empty_decode": 1,
    "truncated": 1,
    "h264_corrupt": 2,
    "macroblock_error": 2,
    "invalid_data": 2,
    "repeated_errors": 2,
    "aac_corrupt": 3,
    "bad_timestamps": 4,
}

_PRIORITY_LABELS = {
    1: ("CRITICAL - Must Redownload", "Files completely broken - cannot be played at all"),
    2: ("HIGH - Should Redownload", "Video quality affected - visual glitches/corruption"),
    3: ("MEDIUM - Consider Redownloading", "Audio issues - brief pops or dropouts, often not noticeable"),
    4: ("LOW - Probably Fine", "Minor technical issues - playback unlikely to be affected"),
}


def _generate_issues_md(cache, files_scanned):
    from collections import defaultdict

    errs = [(k, v) for k, v in cache.items() if v.get("result") == "error"]
    if not errs:
        md_path = REPORTS_DIR / "issues_report.md"
        md_path.write_text("# Jellyfin Library Audit - Issues Report\n\nNo issues found.\n", encoding="utf-8")
        return md_path

    by_show = defaultdict(list)
    for k, v in errs:
        parts = k.split("\\")
        if len(parts) >= 4 and "TV Shows" in parts[1]:
            show, drive, ep = parts[2], parts[0], parts[-1]
        elif len(parts) >= 3 and "Movies" in parts[1]:
            show, drive, ep = parts[2], parts[0], parts[-1]
        else:
            show, drive, ep = "unknown", "?", parts[-1] if parts else k
        etype = _classify_error(v.get("detail", ""))
        by_show[show].append({"ep": ep, "type": etype, "drive": drive})

    all_types = Counter(_classify_error(v.get("detail", "")) for _, v in errs)
    lines = []
    lines.append("# Jellyfin Library Audit - Issues Report")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    lines.append(f"**Total files scanned:** {files_scanned:,}  ")
    lines.append(f"**Total issues found:** {len(errs)} ({len(errs)/files_scanned*100:.1f}%)")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append("| Priority | Description | Files |")
    lines.append("|----------|-------------|-------|")
    for p in sorted(_PRIORITY_LABELS.keys()):
        label, _ = _PRIORITY_LABELS[p]
        count = sum(n for t, n in all_types.items() if _PRIORITY.get(t, 9) == p)
        if count > 0:
            lines.append(f"| **{label}** | {_PRIORITY_LABELS[p][1]} | {count} |")
    lines.append("")

    # Error types
    lines.append("## Error Types")
    lines.append("")
    lines.append("| Type | Count | Severity | Description |")
    lines.append("|------|-------|----------|-------------|")
    for t, n in sorted(all_types.items(), key=lambda x: (_PRIORITY.get(x[0], 9), -x[1])):
        desc = _ERR_DESCRIPTIONS.get(t, "Unknown")
        p = _PRIORITY.get(t, 9)
        sev = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW"}.get(p, "?")
        lines.append(f"| `{t}` | {n} | {sev} | {desc} |")
    lines.append("")

    # Quick reference
    lines.append("---")
    lines.append("")
    lines.append("## Quick Reference - Seasons to Redownload")
    lines.append("")
    lines.append("| Show | Drive | Seasons | Episodes | Severity |")
    lines.append("|------|-------|---------|----------|----------|")

    for show in sorted(by_show.keys(), key=lambda x: -len(by_show[x])):
        eps = by_show[show]
        if len(eps) < 3:
            continue
        drive = eps[0]["drive"]
        seasons = set()
        for e in eps:
            m = re.search(r"S(\d+)", e["ep"])
            if m:
                seasons.add(int(m.group(1)))
        s_str = ", ".join(f"S{s:02d}" for s in sorted(seasons)) if seasons else "various"
        worst = min(_PRIORITY.get(e["type"], 9) for e in eps)
        sev = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW"}.get(worst, "?")
        lines.append(f"| {show} | {drive}: | {s_str} | {len(eps)} | {sev} |")
    lines.append("")

    # Detailed sections by priority
    for p in sorted(_PRIORITY_LABELS.keys()):
        label, desc = _PRIORITY_LABELS[p]
        shows_at_p = {}
        for show, eps in by_show.items():
            matching = [e for e in eps if _PRIORITY.get(e["type"], 9) == p]
            if matching:
                shows_at_p[show] = matching

        if not shows_at_p:
            continue

        lines.append("---")
        lines.append("")
        lines.append(f"## {label}")
        lines.append("")
        lines.append(f"> {desc}")
        lines.append("")

        for show in sorted(shows_at_p.keys(), key=lambda x: -len(shows_at_p[x])):
            eps = shows_at_p[show]
            drive = eps[0]["drive"]
            types = Counter(e["type"] for e in eps)
            type_str = ", ".join(f"`{t}`" for t, _ in types.most_common())
            lines.append(f"### {show} ({drive}: drive) - {len(eps)} files")
            lines.append("")
            lines.append(f"Error types: {type_str}")
            lines.append("")
            for e in sorted(eps, key=lambda x: x["ep"]):
                lines.append(f"- [ ] `{e['type']}` {e['ep']}")
            lines.append("")

    md_path = REPORTS_DIR / "issues_report.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


if __name__ == "__main__":
    main()
