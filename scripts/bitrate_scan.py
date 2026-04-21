#!/usr/bin/env python
"""
bitrate_scan.py - Lightweight bitrate scan of all Jellyfin media libraries.

Runs ffprobe (metadata only, no decode) on every video file and produces:
  - CSV report with per-file bitrate, resolution, duration, codec
  - Summary grouped by show/movie with avg/min/max bitrate
  - Highlights for redownload candidates (low bitrate) and re-encode candidates (high bitrate)

Designed to run alongside other CPU-heavy work — uses below-normal process
priority and single-threaded ffprobe calls (each ~50-100ms).

Run:
  python bitrate_scan.py                  # scan all drives
  python bitrate_scan.py --drive D        # limit to specific drive(s)
  python bitrate_scan.py --low 1500       # flag files below 1500 kbps (default: 2000)
  python bitrate_scan.py --high 8000      # flag files above 8000 kbps (default: 10000)
"""

from pathlib import Path
from datetime import datetime
import argparse
import csv
import json
import os
import subprocess
import sys

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

REPORTS_DIR = Path(__file__).parent / "audit_reports"

# ============================================================


def set_low_priority():
    """Set this process to below-normal priority so we don't compete with the audit."""
    if sys.platform == "win32":
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(),
            BELOW_NORMAL_PRIORITY_CLASS,
        )


def probe_file(path):
    """Run ffprobe on a single file, return dict with bitrate/resolution/codec/duration."""
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
            timeout=30,
            creationflags=0x08000000 if sys.platform == "win32" else 0,  # CREATE_NO_WINDOW
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])

        v_streams = [s for s in streams if s.get("codec_type") == "video"]
        a_streams = [s for s in streams if s.get("codec_type") == "audio"]

        video = v_streams[0] if v_streams else {}
        audio = a_streams[0] if a_streams else {}

        bitrate = int(fmt.get("bit_rate", 0) or 0)
        duration = float(fmt.get("duration", 0) or 0)
        size = int(fmt.get("size", 0) or 0)

        return {
            "bitrate_kbps": bitrate // 1000,
            "duration_min": round(duration / 60, 1),
            "size_mb": round(size / (1024 * 1024), 1),
            "width": video.get("width", 0),
            "height": video.get("height", 0),
            "video_codec": video.get("codec_name", ""),
            "audio_codec": audio.get("codec_name", ""),
            "video_bitrate_kbps": int(video.get("bit_rate", 0) or 0) // 1000,
        }
    except Exception:
        return None


def parse_show_name(path, root, kind):
    """Extract show/movie name from path."""
    try:
        rel = path.relative_to(root)
        parts = rel.parts
        if kind == "tv" and len(parts) >= 1:
            return parts[0]
        elif kind == "movie" and len(parts) >= 1:
            return parts[0]
    except ValueError:
        pass
    return str(path.parent.name)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Scan Jellyfin libraries for bitrate stats.")
    ap.add_argument("--drive", action="append", default=[],
                    help="Limit to specific drives (e.g. --drive D --drive F).")
    ap.add_argument("--low", type=int, default=2000,
                    help="Flag files below this bitrate in kbps (default: 2000).")
    ap.add_argument("--high", type=int, default=10000,
                    help="Flag files above this bitrate in kbps (default: 10000).")
    args = ap.parse_args()

    set_low_priority()

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

    print("=== Bitrate Scan ===")
    print(f"Started:    {datetime.now().isoformat(timespec='seconds')}")
    print(f"Low flag:   <{args.low} kbps")
    print(f"High flag:  >{args.high} kbps")
    print(f"Priority:   below-normal")
    print("====================\n")

    # Collect all files
    all_files = []
    for root, drive in tv_roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                all_files.append((p, drive, "tv", root))
    for root, drive in movie_roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                all_files.append((p, drive, "movie", root))

    print(f"Total files to scan: {len(all_files)}\n")

    # Scan
    results = []
    start_time = datetime.now()
    for i, (path, drive, kind, root) in enumerate(all_files):
        info = probe_file(path)
        show = parse_show_name(path, root, kind)
        row = {
            "drive": drive,
            "kind": kind,
            "show": show,
            "file": path.name,
            "path": str(path),
        }
        if info:
            row.update(info)
        else:
            row.update({
                "bitrate_kbps": 0, "duration_min": 0, "size_mb": 0,
                "width": 0, "height": 0, "video_codec": "", "audio_codec": "",
                "video_bitrate_kbps": 0,
            })
        results.append(row)

        if (i + 1) % 200 == 0:
            elapsed = (datetime.now() - start_time).total_seconds()
            rate = (i + 1) / elapsed
            eta = (len(all_files) - i - 1) / rate
            print(f"[progress] {i+1} / {len(all_files)} ({(i+1)/len(all_files)*100:.1f}%) "
                  f"— {rate:.0f} files/sec — ETA {eta:.0f}s", flush=True)

    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\nScan complete: {len(results)} files in {elapsed:.0f}s "
          f"({len(results)/elapsed:.0f} files/sec)\n")

    # Write CSV
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = REPORTS_DIR / "bitrate_scan.csv"
    fieldnames = ["drive", "kind", "show", "file", "bitrate_kbps", "video_bitrate_kbps",
                  "duration_min", "size_mb", "width", "height", "video_codec", "audio_codec", "path"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in results:
            w.writerow(row)
    print(f"CSV report: {csv_path}\n")

    # === SUMMARY ===
    valid = [r for r in results if r["bitrate_kbps"] > 0]
    if not valid:
        print("No valid bitrate data found.")
        return

    # Overall stats
    bitrates = [r["bitrate_kbps"] for r in valid]
    bitrates.sort()
    avg_br = sum(bitrates) / len(bitrates)
    median_br = bitrates[len(bitrates) // 2]
    print(f"=== OVERALL STATS ===")
    print(f"Files with valid bitrate: {len(valid)}")
    print(f"Average bitrate:  {avg_br:.0f} kbps ({avg_br/1000:.1f} Mbps)")
    print(f"Median bitrate:   {median_br} kbps ({median_br/1000:.1f} Mbps)")
    print(f"Min:              {bitrates[0]} kbps")
    print(f"Max:              {bitrates[-1]} kbps ({bitrates[-1]/1000:.1f} Mbps)")

    # By show — sorted by avg bitrate descending
    from collections import defaultdict
    by_show = defaultdict(list)
    for r in valid:
        by_show[r["show"]].append(r)

    print(f"\n=== TOP 20 HIGHEST BITRATE SHOWS/MOVIES ===\n")
    print(f"{'Show':<45} {'Eps':>4} {'Avg kbps':>9} {'Max kbps':>9} {'Avg MB':>7} {'Res':>10}")
    print("-" * 95)
    sorted_shows = sorted(by_show.items(), key=lambda x: sum(r["bitrate_kbps"] for r in x[1]) / len(x[1]), reverse=True)
    for show, files in sorted_shows[:20]:
        brs = [r["bitrate_kbps"] for r in files]
        avg = sum(brs) / len(brs)
        mx = max(brs)
        avg_size = sum(r["size_mb"] for r in files) / len(files)
        res = f"{files[0]['width']}x{files[0]['height']}"
        print(f"{show[:44]:<45} {len(files):>4} {avg:>8.0f} {mx:>9} {avg_size:>6.0f} {res:>10}")

    print(f"\n=== TOP 20 LOWEST BITRATE SHOWS/MOVIES ===\n")
    print(f"{'Show':<45} {'Eps':>4} {'Avg kbps':>9} {'Min kbps':>9} {'Avg MB':>7} {'Res':>10}")
    print("-" * 95)
    for show, files in sorted_shows[-20:]:
        brs = [r["bitrate_kbps"] for r in files]
        avg = sum(brs) / len(brs)
        mn = min(brs)
        avg_size = sum(r["size_mb"] for r in files) / len(files)
        res = f"{files[0]['width']}x{files[0]['height']}"
        print(f"{show[:44]:<45} {len(files):>4} {avg:>8.0f} {mn:>9} {avg_size:>6.0f} {res:>10}")

    # Flag individual files
    low_files = [r for r in valid if r["bitrate_kbps"] < args.low]
    high_files = [r for r in valid if r["bitrate_kbps"] > args.high]

    if low_files:
        print(f"\n=== LOW BITRATE FILES (<{args.low} kbps) — REDOWNLOAD CANDIDATES ===\n")
        print(f"{'Bitrate':>8} {'Size':>7} {'Res':>10} {'File'}")
        print("-" * 80)
        for r in sorted(low_files, key=lambda x: x["bitrate_kbps"]):
            res = f"{r['width']}x{r['height']}"
            print(f"{r['bitrate_kbps']:>7}k {r['size_mb']:>6.0f}M {res:>10} {r['show']}/{r['file']}")

    if high_files:
        print(f"\n=== HIGH BITRATE FILES (>{args.high} kbps) — RE-ENCODE CANDIDATES ===\n")
        print(f"{'Bitrate':>8} {'Size':>7} {'Res':>10} {'File'}")
        print("-" * 80)
        for r in sorted(high_files, key=lambda x: -x["bitrate_kbps"])[:50]:
            res = f"{r['width']}x{r['height']}"
            print(f"{r['bitrate_kbps']:>7}k {r['size_mb']:>6.0f}M {res:>10} {r['show']}/{r['file']}")
        if len(high_files) > 50:
            print(f"  ... and {len(high_files) - 50} more (see CSV for full list)")

    # Summary path
    summary_path = REPORTS_DIR / "bitrate_summary.txt"
    # Rewrite summary to file
    import io
    buf = io.StringIO()
    buf.write(f"Bitrate Scan — {datetime.now().isoformat(timespec='seconds')}\n")
    buf.write(f"Files scanned: {len(valid)}\n")
    buf.write(f"Avg bitrate: {avg_br:.0f} kbps ({avg_br/1000:.1f} Mbps)\n")
    buf.write(f"Median: {median_br} kbps\n")
    buf.write(f"Low (<{args.low}k): {len(low_files)} files\n")
    buf.write(f"High (>{args.high}k): {len(high_files)} files\n\n")
    buf.write(f"Top 10 highest avg bitrate shows:\n")
    for show, files in sorted_shows[:10]:
        brs = [r["bitrate_kbps"] for r in files]
        avg = sum(brs) / len(brs)
        buf.write(f"  {avg:.0f}k  {show} ({len(files)} eps)\n")
    buf.write(f"\nTop 10 lowest avg bitrate shows:\n")
    for show, files in sorted_shows[-10:]:
        brs = [r["bitrate_kbps"] for r in files]
        avg = sum(brs) / len(brs)
        buf.write(f"  {avg:.0f}k  {show} ({len(files)} eps)\n")
    summary_path.write_text(buf.getvalue(), encoding="utf-8")
    print(f"\nSummary: {summary_path}")


if __name__ == "__main__":
    main()
