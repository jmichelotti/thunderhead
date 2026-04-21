#!/usr/bin/env python
"""
Local HTTP server that receives m3u8 URLs from the HLS Capture browser extension,
resolves show metadata via OMDb, and downloads episodes using yt-dlp with
Jellyfin-friendly naming.

Usage:
    python hls_download_server.py              # dry-run (logs only, no download)
    python hls_download_server.py --apply      # actually download files
    python hls_download_server.py --port 9999  # custom port

Workflow:
    1. Start this server
    2. Load the hls-capture-extension in Vivaldi
    3. Browse to a streaming site, navigate to an episode, press play
    4. Extension captures the m3u8 URL and POSTs it here
    5. Server downloads + names the file into C:\\Temp_Media\\TV Shows\\
"""

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Optional, Dict
from urllib.parse import urlparse

import requests

try:
    from langdetect import detect as _langdetect
    from langdetect import DetectorFactory as _LangDetectFactory
    from langdetect.lang_detect_exception import LangDetectException as _LangDetectException
    _LangDetectFactory.seed = 0  # make results deterministic
    _LANGDETECT_OK = True
except ImportError:
    _LANGDETECT_OK = False


# ========= CONFIG =========

OUTPUT_DIR = Path(r"C:\Temp_Media\TV Shows")
TEMP_DIR = Path(r"C:\Temp_Media\_hls_tmp")
OMDB_API_KEY = "591dfd18"
DEFAULT_PORT = 9876
MAX_HEIGHT = 1080
LOG_FILE = Path(__file__).parent / "hls_server.log"
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5 MB — truncate old entries when exceeded


# ========= LOGGING =========

class TeeWriter:
    """Duplicates writes to both the original stream and a log file."""

    def __init__(self, original, log_file_handle):
        self._original = original
        self._log = log_file_handle

    def write(self, data):
        if self._original:
            try:
                self._original.write(data)
            except Exception:
                pass
        try:
            self._log.write(data)
            self._log.flush()
        except Exception:
            pass

    def flush(self):
        if self._original:
            try:
                self._original.flush()
            except Exception:
                pass
        try:
            self._log.flush()
        except Exception:
            pass

    # Let attribute lookups (fileno, encoding, etc.) fall through to original
    def __getattr__(self, name):
        return getattr(self._original, name)


def _setup_logging():
    """Redirect stdout and stderr to tee into LOG_FILE."""
    # Rotate if log is too big — keep the most recent half
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_SIZE:
        data = LOG_FILE.read_bytes()
        LOG_FILE.write_bytes(data[len(data) // 2:])

    log_fh = open(LOG_FILE, "a", encoding="utf-8")
    log_fh.write(f"\n{'='*60}\n")
    log_fh.write(f"Server started at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    log_fh.write(f"{'='*60}\n")
    log_fh.flush()

    sys.stdout = TeeWriter(sys.__stdout__, log_fh)
    sys.stderr = TeeWriter(sys.__stderr__, log_fh)


# ========= HELPERS =========

def sanitize_for_windows(name: str) -> str:
    invalid_chars = r'<>:"/\\|?*'
    name = name.translate(str.maketrans({ch: " " for ch in invalid_chars}))
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(" .")


_omdb_cache: Dict[str, Optional[Dict[str, str]]] = {}

def try_omdb(params: dict) -> Optional[Dict[str, str]]:
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


def lookup_show(title_guess: str) -> Optional[Dict[str, str]]:
    """Try OMDb exact title match, then search.  Results are cached."""
    if not OMDB_API_KEY or not title_guess:
        return None

    cache_key = title_guess.lower()
    if cache_key in _omdb_cache:
        return _omdb_cache[cache_key]

    # Exact title match
    meta = try_omdb({"apikey": OMDB_API_KEY, "t": title_guess, "type": "series"})
    if meta:
        _omdb_cache[cache_key] = meta
        return meta

    # Search fallback
    meta = try_omdb({"apikey": OMDB_API_KEY, "s": title_guess, "type": "series"})
    if meta:
        _omdb_cache[cache_key] = meta
        return meta

    # If title ends with digits (site slug artifact, e.g. "Paradise4" -> "Paradise"),
    # retry without the trailing number
    stripped = re.sub(r"\s*\d+$", "", title_guess).strip()
    if stripped and stripped != title_guess:
        meta = try_omdb({"apikey": OMDB_API_KEY, "t": stripped, "type": "series"})
        if meta:
            _omdb_cache[cache_key] = meta
            return meta
        meta = try_omdb({"apikey": OMDB_API_KEY, "s": stripped, "type": "series"})
        if meta:
            _omdb_cache[cache_key] = meta
            return meta

    return None


# ========= URL PARSING =========

def parse_show_from_url(page_url: str) -> dict:
    """
    Parse show name, season, and episode from a streaming site page URL.

    Expected URL patterns:
        https://1movies.bz/tv-the-pitt-4vevg#ep=1,5
        https://1movies.bz/tv-show-name-xxxxx#ep=<season>,<episode>
        https://1moviesz.to/watch/movie-dead-poets-society-rylnp#ep=1

    Returns: {show_slug, show_name, season, episode, is_movie} or partial dict.
    """
    result = {"show_slug": "", "show_name": "", "season": None, "episode": None, "is_movie": False}

    parsed = urlparse(page_url)
    path = parsed.path.strip("/")

    # Check for movie URL: "watch/movie-dead-poets-society-rylnp" or "movie-name-xxxxx"
    movie_match = re.search(r"movie-(.+?)(?:-[a-z0-9]{4,6})?$", path, re.IGNORECASE)
    if movie_match:
        slug = movie_match.group(1)
        result["show_slug"] = slug
        result["show_name"] = slug.replace("-", " ").title()
        result["is_movie"] = True
        return result

    # Extract show slug from path: "watch/tv-the-pitt-4vevg" or "tv-the-pitt-4vevg"
    slug_match = re.search(r"tv-(.+?)(?:-[a-z0-9]{4,6})?$", path, re.IGNORECASE)
    if slug_match:
        slug = slug_match.group(1)
        result["show_slug"] = slug
        # Convert slug to title case: "the-pitt" -> "The Pitt"
        result["show_name"] = slug.replace("-", " ").title()

    # Extract season + episode from fragment: "#ep=2,5" -> season=2, episode=5
    fragment = parsed.fragment
    ep_match = re.search(r"ep=(\d+),(\d+)", fragment)
    if ep_match:
        result["season"] = int(ep_match.group(1))
        result["episode"] = int(ep_match.group(2))

    return result


def parse_episode_info(body: dict, page_url: str) -> dict:
    """
    Extract show_name, season, episode from request body or page URL.

    First tries body fields (show_name, season, episode) sent by the extension
    for DOM-based sites like brocoflix.xyz where the URL never changes.
    Falls back to URL parsing for URL-based sites like 1movies.bz.

    Returns: {show_slug, show_name, season, episode, is_movie}
    """
    show_name = (body.get("show_name") or "").strip()
    season = body.get("season")
    episode = body.get("episode")

    # If the extension provided all three fields directly, use them
    if show_name and season is not None and episode is not None:
        try:
            season = int(season)
            episode = int(episode)
            show_slug = re.sub(r"[^a-z0-9]+", "-", show_name.lower()).strip("-")
            return {"show_slug": show_slug, "show_name": show_name,
                    "season": season, "episode": episode, "is_movie": False}
        except (ValueError, TypeError):
            pass

    # Fall back to URL parsing (1movies.bz slug format)
    url_info = parse_show_from_url(page_url)
    is_movie = url_info.get("is_movie", False) or "type=movie" in page_url
    return {
        "show_slug": url_info.get("show_slug", ""),
        "show_name": show_name or url_info.get("show_name", ""),
        "season":    season  if season  is not None else url_info.get("season"),
        "episode":   episode if episode is not None else url_info.get("episode"),
        "is_movie":  is_movie,
    }


# ========= DOWNLOAD PROGRESS TRACKING =========

# Shared download state: {ep_key: {filename, status, percent, speed, eta, frag, total_frags, quality, size}}
_downloads: Dict[str, dict] = {}
_downloads_lock = threading.Lock()

# yt-dlp progress line pattern:
# [download]  42.3% of ~ 500.00MiB at  5.23MiB/s ETA 01:23 (frag 381/900)
_PROGRESS_RE = re.compile(
    r"\[download\]\s+"
    r"(?P<pct>[\d.]+)%\s+"
    r"of\s+~?\s*(?P<size>\S+)\s+"
    r"at\s+(?P<speed>\S+)\s+"
    r"ETA\s+(?P<eta>\S+)\s+"
    r"\(frag\s+(?P<frag>\d+)/(?P<total>\d+)\)"
)
# [info] ...: Downloading 1 format(s): 4500
_QUALITY_RE = re.compile(r"Downloading \d+ format\(s\):\s*(?P<quality>\S+)")
# [download] 100% ...
_DONE_RE = re.compile(r"\[download\]\s+100%")
# Total fragments
_FRAGS_RE = re.compile(r"Total fragments:\s*(?P<total>\d+)")


def update_download(ep_key: str, **kwargs):
    with _downloads_lock:
        if ep_key in _downloads:
            _downloads[ep_key].update(kwargs)


def fix_metadata_for_jellyfin(mp4_path: Path, label: str = "") -> bool:
    """Remux an MP4 to fix broken encoder metadata (hls.js / dailymotion tags).

    Rewrites the file in-place using stream-copy (no re-encode, near-instant).
    Returns True if the file was fixed (or was already fine), False on error.
    """
    ENCODER_PATTERNS = ["hls.js", "dailymotion"]

    # Probe the file for encoder tags
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(mp4_path),
    ]
    try:
        result = subprocess.run(
            probe_cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        info = json.loads(result.stdout)
    except Exception as e:
        print(f"  {label} [metadata fix] ffprobe failed: {e}", flush=True)
        return True  # non-fatal, skip fix

    # Check format-level and stream-level encoder tags
    needs_fix = False
    for tags_source in [info.get("format", {})] + info.get("streams", []):
        enc = (tags_source.get("tags", {}) or {}).get("encoder", "").lower()
        if any(p in enc for p in ENCODER_PATTERNS):
            needs_fix = True
            break

    if not needs_fix:
        return True

    # Remux in-place: copy to temp, replace original
    tmp_path = mp4_path.with_suffix(".fixing.mp4")
    remux_cmd = [
        "ffmpeg", "-y",
        "-i", str(mp4_path),
        "-map", "0",
        "-c", "copy",
        "-movflags", "+faststart",
        str(tmp_path),
    ]
    print(f"  {label} [metadata fix] Remuxing to strip bad encoder tags...", flush=True)
    try:
        proc = subprocess.run(remux_cmd, capture_output=True, text=True, timeout=300,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.returncode != 0:
            print(f"  {label} [metadata fix] ffmpeg failed (exit {proc.returncode})", flush=True)
            tmp_path.unlink(missing_ok=True)
            return False
        # Replace original with fixed file
        mp4_path.unlink()
        tmp_path.rename(mp4_path)
        print(f"  {label} [metadata fix] Done — bad encoder tags stripped", flush=True)
        return True
    except Exception as e:
        print(f"  {label} [metadata fix] Error: {e}", flush=True)
        tmp_path.unlink(missing_ok=True)
        return False


def download_m3u8(m3u8_url: str, output_path: Path, dry_run: bool, ep_key: str) -> str:
    """Download an m3u8 stream using yt-dlp. Returns status message."""
    if dry_run:
        update_download(ep_key, status="dry_run")
        return f"[DRY RUN] Would download to {output_path}"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    # Download to short temp path to avoid Windows long-path issues with fragments
    temp_file = TEMP_DIR / output_path.name

    cmd = [
        "yt-dlp",
        "-f", f"bestvideo[ext=mp4][height<={MAX_HEIGHT}]/"
              f"bv*[height<={MAX_HEIGHT}]+ba/best",
        "--merge-output-format", "mp4",
        "--postprocessor-args", "ffmpeg:-movflags +faststart",
        "--no-write-subs",
        "--newline",  # Force one progress line per update (no \r overwrites)
        "-o", str(temp_file),
        m3u8_url,
    ]

    update_download(ep_key, status="downloading")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW
    )

    # Collect all output lines for diagnostics on failure
    all_output = []

    for line in proc.stdout:
        line = line.rstrip()
        all_output.append(line)

        # Parse quality format
        m = _QUALITY_RE.search(line)
        if m:
            update_download(ep_key, quality=m.group("quality"))

        # Parse total fragments
        m = _FRAGS_RE.search(line)
        if m:
            update_download(ep_key, total_frags=int(m.group("total")))

        # Parse progress (updates popup UI, no console output)
        m = _PROGRESS_RE.search(line)
        if m:
            update_download(
                ep_key,
                percent=float(m.group("pct")),
                size=m.group("size"),
                speed=m.group("speed"),
                eta=m.group("eta"),
                frag=int(m.group("frag")),
                total_frags=int(m.group("total")),
            )

        # Check for 100% done
        if _DONE_RE.search(line):
            update_download(ep_key, percent=100.0)

    proc.wait()

    if proc.returncode != 0:
        update_download(ep_key, status="error")
        # Dump full yt-dlp output for diagnosis
        print(f"  [{ep_key}] yt-dlp FAILED (exit {proc.returncode}) — full output:", flush=True)
        for out_line in all_output:
            print(f"    | {out_line}", flush=True)
        return f"yt-dlp failed (exit {proc.returncode})"

    # Move from temp to final location (video + any subtitle files)
    update_download(ep_key, status="moving")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(temp_file), str(output_path))

    # Move any .srt files that match the video name (skip thumbnail sprites)
    for sub_file in TEMP_DIR.glob(f"{temp_file.stem}*.srt"):
        content = sub_file.read_text(encoding="utf-8", errors="ignore")
        if "xywh=" in content or "thumbnails" in content.lower():
            sub_file.unlink()
            continue
        sub_dest = output_path.parent / sub_file.name
        shutil.move(str(sub_file), str(sub_dest))

    # Fix broken encoder metadata (hls.js tags) before marking done
    fix_metadata_for_jellyfin(output_path, label=f"[{ep_key}]")

    update_download(ep_key, status="done", percent=100.0)
    return "downloaded"


# ========= SUBTITLE HANDLING =========

# Pending subtitles keyed by ep_key, waiting for video download to determine final path
# {ep_key: [subtitle_url, ...]}
_pending_subs: Dict[str, list] = {}
_pending_subs_lock = threading.Lock()

# Resolved output dirs keyed by ep_key: {ep_key: (show_title, season, episode)}
_resolved_episodes: Dict[str, tuple] = {}
_resolved_lock = threading.Lock()

# Episodes that already have a saved subtitle — prevents race where multiple
# threads pass the file-exists check before any have written the file
_saved_subs: set = set()
_saved_subs_lock = threading.Lock()


def vtt_to_srt(vtt_text: str) -> str:
    """Convert WebVTT content to SRT format."""
    # Strip VTT header
    lines = vtt_text.strip().splitlines()
    output = []
    cue_num = 0
    i = 0

    # Skip header lines (WEBVTT and any metadata before first blank line)
    while i < len(lines) and lines[i].strip():
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1

    while i < len(lines):
        # Skip blank lines
        if not lines[i].strip():
            i += 1
            continue

        # Look for timestamp line (contains " --> ")
        # Skip any cue ID line that might precede it
        timestamp_line = None
        if " --> " in lines[i]:
            timestamp_line = lines[i]
        elif i + 1 < len(lines) and " --> " in lines[i + 1]:
            i += 1  # skip cue ID
            timestamp_line = lines[i]
        else:
            i += 1
            continue

        # Convert VTT timestamps (may have . instead of ,) to SRT format
        timestamp_line = timestamp_line.replace(".", ",")
        # Remove any positioning info after timestamps
        timestamp_line = re.sub(r"([\d:,]+\s*-->\s*[\d:,]+).*", r"\1", timestamp_line)
        # Ensure HH:MM:SS,mmm format (VTT may omit hours)
        parts = timestamp_line.split(" --> ")
        fixed_parts = []
        for part in parts:
            part = part.strip()
            if part.count(":") == 1:
                part = "00:" + part
            fixed_parts.append(part)
        timestamp_line = " --> ".join(fixed_parts)

        cue_num += 1
        i += 1

        # Collect text lines until blank line or end
        text_lines = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i])
            i += 1

        if text_lines:
            output.append(f"{cue_num}")
            output.append(timestamp_line)
            output.extend(text_lines)
            output.append("")

    return "\n".join(output)


def is_english_subtitle(text: str) -> tuple[bool, str]:
    """
    Heuristic: check if subtitle text is predominantly English.
    Returns (accepted, reason) where reason is empty string on acceptance
    or a short description of why it was rejected.
    """
    # Extract just the dialogue lines (skip timestamps, cue numbers, headers)
    dialogue = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if re.match(r"^\d+$", line):  # cue number
            continue
        if "-->" in line:  # timestamp
            continue
        dialogue.append(line)

    if not dialogue:
        return False, "no dialogue lines"

    # Sample first 50 dialogue lines for better accuracy
    sample = " ".join(dialogue[:50])
    if not sample:
        return False, "empty sample"

    # Reject thumbnail sprite maps (VTT files with xywh= coordinates, not dialogue)
    if "xywh=" in sample or "thumbnails" in sample.lower():
        return False, "thumbnail sprite"

    # Check for UTF-8 mojibake (Latin text decoded as cp1252/latin1 then re-encoded)
    # e.g. "Ã©" (é), "Ã¨" (è), "Ã´" (ô), "Ã§" (ç) — common in French/Spanish/etc.
    mojibake_count = len(re.findall(r"Ã[\x80-\xbf]", sample))
    if mojibake_count > 2:
        return False, f"mojibake ({mojibake_count} hits)"

    # Check for CP1250/Latin-1 encoding corruption: characters in U+0080–U+00BF
    # (like ³ ¹ ¿ ¯ ± ¶) are Latin-1 supplement symbols that never appear in clean
    # dialogue text. When Polish/Czech CP1250 is decoded as Latin-1, accented letters
    # become these symbols (e.g. ł→³, ż→¿, ą→¹). More than 2 = encoding corruption.
    latin1_symbols = sum(1 for c in sample if 0x80 <= ord(c) <= 0xBF)
    if latin1_symbols > 2:
        return False, f"latin1 symbols ({latin1_symbols})"

    # Check for actual Unicode accented Latin characters (common in non-English European)
    ACCENTED_RANGES = (
        (0x00C0, 0x00D6),  # À-Ö
        (0x00D8, 0x00F6),  # Ø-ö
        (0x00F8, 0x00FF),  # ø-ÿ
        (0x0100, 0x017F),  # Latin Extended-A
        (0x0180, 0x024F),  # Latin Extended-B
    )
    accented = sum(
        1 for c in sample
        if any(lo <= ord(c) <= hi for lo, hi in ACCENTED_RANGES)
    )
    alpha = sum(1 for c in sample if c.isalpha())
    if alpha > 0 and accented / alpha > 0.05:
        return False, f"accented ratio ({accented}/{alpha} = {accented/alpha:.2f})"

    # Detect non-English by common foreign words/patterns
    sample_lower = sample.lower()
    foreign_markers = [
        # French
        r"\bje\b", r"\bqu['e]", r"\bc'est\b", r"\bune?\b", r"\bpour\b",
        r"\bpas\b", r"\bvous\b", r"\bles\b", r"\bdes\b", r"\bdans\b",
        # Spanish
        r"\bel\b", r"\blos\b", r"\bpor\b", r"\bque\b", r"\buna\b",
        r"\bestá\b", r"\bcomo\b",
        # German
        r"\bich\b", r"\bein\b", r"\bdas\b", r"\bist\b", r"\bnicht\b",
        r"\baber\b",
        # Portuguese
        r"\bnão\b", r"\bcom\b", r"\buma\b", r"\bpara\b", r"\bvocê\b",
    ]
    foreign_hits = sum(
        len(re.findall(pat, sample_lower)) for pat in foreign_markers
    )
    if foreign_hits > 8:
        return False, f"foreign words ({foreign_hits} hits)"

    # Basic ASCII ratio check
    ascii_letters = sum(1 for c in sample if c.isascii() and c.isalpha())
    all_letters = sum(1 for c in sample if c.isalpha())
    if all_letters == 0:
        return False, "no letters"
    ascii_ratio = ascii_letters / all_letters
    if ascii_ratio <= 0.9:
        return False, f"ascii ratio ({ascii_ratio:.2f})"

    # Final check: langdetect confirms the language is actually English.
    # Catches Latin-script languages (Fijian, Indonesian, Swahili, Turkish, etc.)
    # that pass all character-based checks above.
    # Falls through gracefully if langdetect is not installed or sample is too short.
    if _LANGDETECT_OK:
        try:
            detected = _langdetect(sample)
            if detected != "en":
                return False, f"langdetect={detected}"
        except _LangDetectException:
            pass  # too short or ambiguous — trust the other checks

    return True, ""


def download_subtitle(subtitle_url: str, ep_key: str):
    """Download a subtitle file and save it next to the video (English only)."""
    with _resolved_lock:
        episode_info = _resolved_episodes.get(ep_key)

    if not episode_info:
        # Video hasn't been processed yet, queue for later
        with _pending_subs_lock:
            _pending_subs.setdefault(ep_key, []).append(subtitle_url)
        return

    show_title, season, episode = episode_info
    if season is None or episode is None:
        # Movie
        ep_tag = "Movie"
        srt_name = f"{show_title}.srt"
        srt_path = MOVIE_OUTPUT_DIR / show_title / srt_name
    else:
        ep_tag = f"S{season:02d}E{episode:02d}"
        srt_name = f"{show_title} {ep_tag}.srt"
        srt_path = OUTPUT_DIR / show_title / f"Season {season:02d}" / srt_name

    # Check if subtitle already saved (file on disk or claimed by another thread)
    with _saved_subs_lock:
        if ep_key in _saved_subs or srt_path.exists():
            return

    try:
        resp = requests.get(subtitle_url, timeout=15)
        resp.raise_for_status()
        # Force UTF-8 decoding — VTT files are almost always UTF-8, but
        # requests may guess latin-1 from headers, mangling multibyte chars
        # like ♪ (U+266A) into separate bytes (â/\x99/ª)
        resp.encoding = "utf-8"
        content = resp.text

        # Check if English before saving
        accepted, reason = is_english_subtitle(content)
        if not accepted:
            return

        # Claim this episode under lock so only the first English subtitle wins
        with _saved_subs_lock:
            if ep_key in _saved_subs:
                return
            _saved_subs.add(ep_key)

        # Convert VTT to SRT if needed
        if subtitle_url.lower().endswith(".vtt") or content.strip().startswith("WEBVTT"):
            content = vtt_to_srt(content)

        srt_path.parent.mkdir(parents=True, exist_ok=True)
        srt_path.write_text(content, encoding="utf-8")
        print(f"  [{ep_tag}] Subtitle saved — {srt_path.name}", flush=True)
    except Exception as e:
        print(f"  [{ep_tag}] Subtitle failed: {e}", flush=True)


def process_pending_subs(ep_key: str):
    """Process any subtitles that arrived before the video was resolved."""
    with _pending_subs_lock:
        urls = _pending_subs.pop(ep_key, [])

    for url in urls:
        download_subtitle(url, ep_key)


# ========= BROCOFLIX SESSION MANAGEMENT =========

MOVIE_OUTPUT_DIR = Path(r"C:\Temp_Media\Movies")

_brocoflix_sessions: Dict[str, dict] = {}
_brocoflix_lock = threading.Lock()


def brocoflix_start(body: dict) -> dict:
    """Create a BrocoFlix download session. Returns session info.

    Saves to the root staging dir with just the title (no year, no subfolders).
    fix_file_names.py runs after mux to do OMDb lookup, add year, and move
    into the correct folder structure.
    """
    page_url = body.get("page_url", "")
    info = parse_episode_info(body, page_url)

    show_name = info.get("show_name", "").strip()
    if not show_name:
        return {"status": "error", "message": "missing show_name"}

    show_title = sanitize_for_windows(show_name)

    season = info.get("season")
    episode = info.get("episode")
    is_movie = "type=movie" in page_url

    if is_movie or season is None or episode is None:
        # Movie → C:\Temp_Media\Movies\Title.mp4
        ep_tag = ""
        filename = f"{show_title}.mp4"
        output_path = MOVIE_OUTPUT_DIR / filename
    else:
        # TV episode → C:\Temp_Media\TV Shows\Title SxxExx.mp4
        ep_tag = f"S{season:02d}E{episode:02d}"
        filename = f"{show_title} {ep_tag}.mp4"
        output_path = OUTPUT_DIR / filename

    show_slug = info.get("show_slug", "")
    ep_key = f"{show_slug}|{season or 0}|{episode or 0}"

    # Deduplicate
    with HLSHandler._lock:
        if ep_key in HLSHandler.seen_urls:
            return {"status": "skipped", "message": "duplicate episode"}
        HLSHandler.seen_urls.add(ep_key)

    session_id = f"bf_{int(time.time() * 1000)}_{id(body) & 0xFFFF:04x}"
    temp_path = TEMP_DIR / f"{session_id}.ts"
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    with _brocoflix_lock:
        _brocoflix_sessions[session_id] = {
            "temp_path": temp_path,
            "output_path": output_path,
            "ep_key": ep_key,
            "filename": filename,
            "show_title": show_title,
            "ep_tag": ep_tag,
            "season": season,
            "episode": episode,
            "is_movie": is_movie or season is None or episode is None,
            "chunks_received": 0,
            "total_chunks": 0,
            "bytes_received": 0,
        }

    # Register resolved episode for subtitle matching
    if season is not None and episode is not None:
        with _resolved_lock:
            _resolved_episodes[ep_key] = (show_title, season, episode)

    # Register in downloads tracker
    with _downloads_lock:
        _downloads[ep_key] = {
            "ep_key": ep_key,
            "filename": filename,
            "show": show_title,
            "ep_tag": ep_tag,
            "status": "uploading",
            "percent": 0.0,
            "speed": "",
            "eta": "",
            "frag": 0,
            "total_frags": 0,
            "quality": "",
            "size": "",
            "started": time.time(),
        }

    label = f"[{ep_tag}]" if ep_tag else "[MOVIE]"
    print(f"  {label} BrocoFlix session started — {show_title}", flush=True)

    return {
        "status": "ok",
        "session_id": session_id,
        "ep_key": ep_key,
        "filename": filename,
        "show_title": show_title,
    }


def brocoflix_chunk(session_id: str, chunk_index: int, total_chunks: int,
                    data: bytes) -> dict:
    """Append a TS chunk to the session's temp file."""
    with _brocoflix_lock:
        session = _brocoflix_sessions.get(session_id)
        if not session:
            return {"status": "error", "message": "unknown session"}

    temp_path = session["temp_path"]
    ep_key = session["ep_key"]

    # Append binary data to temp .ts file
    with open(temp_path, "ab") as f:
        f.write(data)

    with _brocoflix_lock:
        session["chunks_received"] += 1
        session["total_chunks"] = total_chunks
        session["bytes_received"] += len(data)
        chunks_received = session["chunks_received"]
        bytes_received = session["bytes_received"]

    # Progress logging every 50 chunks + first + last
    if chunks_received == 1 or chunks_received % 50 == 0 or chunks_received == total_chunks:
        size_mb = bytes_received / (1024 * 1024)
        label = f"[{session.get('ep_tag', '')}]" if session.get("ep_tag") else "[MOVIE]"
        print(f"  {label} BrocoFlix chunk {chunks_received}/{total_chunks} "
              f"({size_mb:.1f}MB received)", flush=True)

    # Update download progress
    pct = (chunks_received / total_chunks * 100) if total_chunks > 0 else 0
    size_mb = bytes_received / (1024 * 1024)
    update_download(ep_key,
                    status="uploading",
                    percent=round(pct, 1),
                    frag=chunks_received,
                    total_frags=total_chunks,
                    size=f"{size_mb:.1f}MiB")

    return {"status": "ok"}


def brocoflix_done(session_id: str, dry_run: bool) -> dict:
    """Mux the collected TS chunks into an MP4 and move to output dir."""
    with _brocoflix_lock:
        session = _brocoflix_sessions.pop(session_id, None)
        if not session:
            return {"status": "error", "message": "unknown session"}

    temp_path: Path = session["temp_path"]
    output_path: Path = session["output_path"]
    ep_key = session["ep_key"]
    label = f"[{session['ep_tag']}]" if session["ep_tag"] else "[MOVIE]"

    if not temp_path.exists():
        update_download(ep_key, status="error")
        return {"status": "error", "message": "temp file missing"}

    size_mb = temp_path.stat().st_size / (1024 * 1024)
    print(f"  {label} BrocoFlix upload complete — {session['chunks_received']} chunks, "
          f"{size_mb:.1f}MB", flush=True)

    if dry_run:
        temp_path.unlink(missing_ok=True)
        update_download(ep_key, status="dry_run", percent=100.0)
        return {"status": "ok", "message": f"[DRY RUN] Would save to {output_path}"}

    # Mux TS -> MP4 with ffmpeg
    update_download(ep_key, status="muxing", percent=100.0)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(temp_path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  {label} Muxing TS -> MP4...", flush=True)

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    temp_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        print(f"  {label} ffmpeg FAILED (exit {proc.returncode}):", flush=True)
        for line in proc.stderr.splitlines()[-10:]:
            print(f"    | {line}", flush=True)
        update_download(ep_key, status="error")
        return {"status": "error", "message": "ffmpeg mux failed"}

    # Fix broken encoder metadata (hls.js tags) before marking done
    fix_metadata_for_jellyfin(output_path, label=label)

    # Run fix_file_names.py to do OMDb lookup, add year, and move into
    # the correct folder structure (Title (Year)/Season XX/ for TV,
    # Title (Year)/ for movies).
    fix_script = Path(__file__).resolve().parent / "fix_file_names.py"
    if fix_script.exists():
        print(f"  {label} Running fix_file_names.py...", flush=True)
        try:
            fix_proc = subprocess.run(
                [sys.executable, str(fix_script)],
                capture_output=True, text=True, timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if fix_proc.returncode == 0:
                print(f"  {label} fix_file_names.py completed", flush=True)
            else:
                print(f"  {label} fix_file_names.py failed (exit {fix_proc.returncode})", flush=True)
                for line in fix_proc.stderr.splitlines()[-5:]:
                    print(f"    | {line}", flush=True)
        except Exception as e:
            print(f"  {label} fix_file_names.py error: {e}", flush=True)

    update_download(ep_key, status="done", percent=100.0)
    print(f"  {label} Done — {output_path}", flush=True)
    return {"status": "ok", "filename": session["filename"]}


def brocoflix_abort(session_id: str) -> dict:
    """Clean up a failed BrocoFlix session."""
    with _brocoflix_lock:
        session = _brocoflix_sessions.pop(session_id, None)
    if session:
        temp_path: Path = session["temp_path"]
        temp_path.unlink(missing_ok=True)
        update_download(session["ep_key"], status="error")
    return {"status": "ok"}


# ========= HTTP SERVER =========

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Suppress noisy connection-abort errors from polling clients."""
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return  # browser closed the connection before we finished — harmless
        super().handle_error(request, client_address)


def probe_formats(m3u8_url: str) -> list:
    """Run yt-dlp -F to list available formats without downloading."""
    cmd = ["yt-dlp", "-F", "--no-download", m3u8_url]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        formats = []
        for line in result.stdout.splitlines():
            # Parse format lines like: "4500 mp4 1920x1080 ..."
            m = re.match(
                r"^(\S+)\s+(mp4|webm|mhtml|\w+)\s+(\d+x\d+|\w+)?\s*(.*)",
                line.strip()
            )
            if m and "x" in (m.group(3) or ""):
                fmt_id = m.group(1)
                ext = m.group(2)
                resolution = m.group(3) or ""
                rest = m.group(4).strip()
                formats.append({
                    "format_id": fmt_id,
                    "ext": ext,
                    "resolution": resolution,
                    "detail": rest,
                })
        return formats
    except Exception as e:
        print(f"  Format probe failed: {e}", flush=True)
        return []


def get_best_format_label(formats: list) -> str:
    """Return a human-readable label for the best format that would be selected."""
    if not formats:
        return "unknown"
    # Find highest resolution
    best = None
    best_pixels = 0
    for f in formats:
        res = f.get("resolution", "")
        if "x" in res:
            try:
                w, h = res.split("x")
                pixels = int(w) * int(h)
                if pixels > best_pixels and int(h) <= MAX_HEIGHT:
                    best_pixels = pixels
                    best = f
            except ValueError:
                pass
    if best:
        return f"{best['resolution']} {best['ext']}"
    return formats[-1].get("resolution", "unknown")


class HLSHandler(BaseHTTPRequestHandler):
    dry_run = True
    seen_urls = set()
    _lock = threading.Lock()

    def log_message(self, format, *args):
        msg = format % args
        # Suppress noisy/redundant HTTP request logs
        if any(s in msg for s in ("GET /status", "GET /downloads", "GET /clear",
                                   "POST /capture", "POST /subtitle",
                                   "POST /preview", "POST /season-info",
                                   "POST /brocoflix-", "OPTIONS")):
            return
        print(f"  [{self.client_address[0]}] {msg}", flush=True)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Session-Id, X-Chunk-Index, X-Total-Chunks")
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            self._send_json({"status": "ok", "dry_run": self.dry_run})
        elif self.path == "/downloads":
            with _downloads_lock:
                self._send_json({"downloads": list(_downloads.values())})
        elif self.path == "/clear":
            with _downloads_lock:
                _downloads.clear()
            with HLSHandler._lock:
                HLSHandler.seen_urls.clear()
            self._send_json({"status": "ok"})
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_subtitle(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json({"status": "error", "message": str(e)}, 400)
            return

        subtitle_url = body.get("subtitle_url", "")
        page_url = body.get("page_url", "")

        if not subtitle_url:
            self._send_json({"status": "error", "message": "missing subtitle_url"}, 400)
            return

        info = parse_episode_info(body, page_url)
        is_movie = info.get("is_movie", False)
        if not info["show_slug"]:
            self._send_json({"status": "skipped", "message": "cannot parse show"})
            return
        if not is_movie and (info["season"] is None or info["episode"] is None):
            self._send_json({"status": "skipped", "message": "cannot parse episode"})
            return

        if is_movie:
            ep_key = f"{info['show_slug']}|movie"
        else:
            ep_key = f"{info['show_slug']}|{info['season']}|{info['episode']}"

        self._send_json({"status": "ok"})

        # Download in background
        threading.Thread(
            target=download_subtitle, args=(subtitle_url, ep_key), daemon=True
        ).start()

    def _handle_preview(self):
        """Return show info + quality without starting a download."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json({"status": "error", "message": str(e)}, 400)
            return

        m3u8_url = body.get("m3u8_url", "")
        page_url = body.get("page_url", "")

        if not m3u8_url:
            self._send_json({"status": "error", "message": "missing m3u8_url"}, 400)
            return

        # Parse show info
        info = parse_episode_info(body, page_url)
        is_movie = info.get("is_movie", False)

        if not info["show_name"]:
            self._send_json({
                "status": "error",
                "message": "Could not parse show name from page URL"
            })
            return

        if not is_movie and (info["season"] is None or info["episode"] is None):
            self._send_json({
                "status": "error",
                "message": "Could not parse show/season/episode from page URL"
            })
            return

        # OMDb lookup
        meta = lookup_show(info["show_name"])
        if meta:
            show_title = sanitize_for_windows(f"{meta['title']} ({meta['year']})")
        else:
            show_title = sanitize_for_windows(info["show_name"])

        if is_movie:
            ep_tag = "Movie"
            filename = f"{show_title}.mp4"
        else:
            season = info["season"]
            episode = info["episode"]
            ep_tag = f"S{season:02d}E{episode:02d}"
            filename = f"{show_title} {ep_tag}.mp4"

        # Use browser-provided quality if available (BrocoFlix CDN 403s yt-dlp)
        browser_quality = body.get("quality")
        if browser_quality:
            best_quality = f"{browser_quality} mp4"
            formats = []
        else:
            formats = probe_formats(m3u8_url)
            best_quality = get_best_format_label(formats)

        self._send_json({
            "status": "ok",
            "show_title": show_title,
            "season": info.get("season"),
            "episode": info.get("episode"),
            "ep_tag": ep_tag,
            "filename": filename,
            "quality": best_quality,
            "formats": formats,
            "is_movie": is_movie,
        })

    def _handle_season_info(self):
        """Log discovered episode list at the start of each season in auto-capture."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json({"status": "error", "message": str(e)}, 400)
            return

        show_name = body.get("show_name", "Unknown")
        season = body.get("season")
        episodes = body.get("episodes", [])

        if season is not None and episodes:
            # Build compact episode summary
            ep_nums = []
            for ep in episodes:
                start = ep.get("epStart")
                end = ep.get("epEnd")
                if start is not None and end is not None and end > start:
                    ep_nums.append(f"{start}-{end}")
                elif start is not None:
                    ep_nums.append(str(start))
            ep_list = ", ".join(ep_nums)
            print(f"\n  [AUTO-CAPTURE] Season S{season:02d} — {show_name}", flush=True)
            print(f"  [AUTO-CAPTURE] {len(episodes)} episodes to capture: [{ep_list}]", flush=True)
            print(flush=True)

        self._send_json({"status": "ok"})

    def _read_json(self):
        """Read and parse JSON body. Returns dict or None on error."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json({"status": "error", "message": f"bad request: {e}"}, 400)
            return None

    def do_POST(self):
        if self.path == "/subtitle":
            self._handle_subtitle()
            return
        if self.path == "/preview":
            self._handle_preview()
            return
        if self.path == "/season-info":
            self._handle_season_info()
            return
        if self.path == "/brocoflix-start":
            body = self._read_json()
            if body is None:
                return
            result = brocoflix_start(body)
            self._send_json(result)
            return
        if self.path == "/brocoflix-chunk":
            session_id = self.headers.get("X-Session-Id", "")
            chunk_index = int(self.headers.get("X-Chunk-Index", "0"))
            total_chunks = int(self.headers.get("X-Total-Chunks", "0"))
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length)  # raw binary
            result = brocoflix_chunk(session_id, chunk_index, total_chunks, data)
            self._send_json(result)
            return
        if self.path == "/brocoflix-done":
            body = self._read_json()
            if body is None:
                return
            result = brocoflix_done(body.get("session_id", ""), self.dry_run)
            # Process any pending subtitles for this episode
            with _brocoflix_lock:
                session = _brocoflix_sessions.get(body.get("session_id", ""))
            ep_key = body.get("ep_key", "")
            if ep_key:
                threading.Thread(
                    target=process_pending_subs, args=(ep_key,), daemon=True
                ).start()
            self._send_json(result)
            return
        if self.path == "/brocoflix-abort":
            body = self._read_json()
            if body is None:
                return
            result = brocoflix_abort(body.get("session_id", ""))
            self._send_json(result)
            return
        if self.path != "/capture":
            self._send_json({"error": "not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send_json({"status": "error", "message": f"bad request: {e}"}, 400)
            return

        m3u8_url = body.get("m3u8_url", "")
        page_url = body.get("page_url", "")

        if not m3u8_url:
            self._send_json({"status": "error", "message": "missing m3u8_url"}, 400)
            return

        # Parse show info from body fields or page URL
        info = parse_episode_info(body, page_url)

        is_movie = info.get("is_movie", False)

        if not info["show_name"]:
            msg = "Could not parse show name from page URL"
            print(f"  ERROR: {msg} — {page_url}", flush=True)
            self._send_json({"status": "error", "message": msg})
            return

        if not is_movie and (info["season"] is None or info["episode"] is None):
            msg = "Could not parse show/season/episode from page URL"
            print(f"  ERROR: {msg} — {page_url}", flush=True)
            self._send_json({"status": "error", "message": msg})
            return

        # Deduplicate by episode (not by m3u8 URL, since players request multiple variants)
        if is_movie:
            ep_key = f"{info['show_slug']}|movie"
        else:
            ep_key = f"{info['show_slug']}|{info['season']}|{info['episode']}"
        with HLSHandler._lock:
            if ep_key in HLSHandler.seen_urls:
                self._send_json({"status": "skipped", "message": "duplicate episode"})
                return
            HLSHandler.seen_urls.add(ep_key)

        # OMDb lookup
        meta = lookup_show(info["show_name"])
        if meta:
            show_title = sanitize_for_windows(f"{meta['title']} ({meta['year']})")
        else:
            show_title = sanitize_for_windows(info["show_name"])

        if is_movie:
            ep_tag = "Movie"
            filename = f"{show_title}.mp4"
            output_path = MOVIE_OUTPUT_DIR / show_title / filename
        else:
            season = info["season"]
            episode = info["episode"]
            ep_tag = f"S{season:02d}E{episode:02d}"
            filename = f"{show_title} {ep_tag}.mp4"
            output_path = OUTPUT_DIR / show_title / f"Season {season:02d}" / filename

        omdb_note = "" if meta else " (OMDb miss)"
        m3u8_domain = urlparse(m3u8_url).hostname or "unknown"
        print(f"  [{ep_tag}] Starting — {show_title}{omdb_note}", flush=True)
        print(f"  [{ep_tag}] m3u8 domain: {m3u8_domain}", flush=True)
        print(f"  [{ep_tag}] m3u8 URL: {m3u8_url[:200]}", flush=True)
        print(f"  [{ep_tag}] page URL: {page_url}", flush=True)

        # Register resolved episode so subtitles know where to save
        with _resolved_lock:
            if is_movie:
                _resolved_episodes[ep_key] = (show_title, None, None)
            else:
                _resolved_episodes[ep_key] = (show_title, season, episode)

        # Process any subtitles that arrived before this video capture
        threading.Thread(
            target=process_pending_subs, args=(ep_key,), daemon=True
        ).start()

        # Register download in progress tracker
        with _downloads_lock:
            _downloads[ep_key] = {
                "ep_key": ep_key,
                "filename": filename,
                "show": show_title,
                "ep_tag": ep_tag,
                "status": "queued",
                "percent": 0.0,
                "speed": "",
                "eta": "",
                "frag": 0,
                "total_frags": 0,
                "quality": "",
                "size": "",
                "started": time.time(),
            }

        # Respond immediately, download in background
        self._send_json({"status": "downloading", "message": filename})

        def do_download():
            result = download_m3u8(m3u8_url, output_path, self.dry_run, ep_key)
            if result == "downloaded":
                print(f"  [{ep_tag}] Done — {output_path}", flush=True)
            else:
                print(f"  [{ep_tag}] {result}", flush=True)

        thread = threading.Thread(target=do_download, daemon=True)
        thread.start()


# ========= MAIN =========

def main():
    parser = argparse.ArgumentParser(
        description="Local server for HLS capture extension"
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually download files (default: dry-run)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to listen on (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    _setup_logging()

    HLSHandler.dry_run = not args.apply

    mode = "LIVE" if args.apply else "DRY RUN"
    print(f"HLS Download Server [{mode}]", flush=True)
    print(f"  Listening on http://localhost:{args.port}", flush=True)
    print(f"  Output dir:  {OUTPUT_DIR}", flush=True)
    print(f"  Ctrl+C to stop\n", flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), HLSHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
