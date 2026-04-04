#!/usr/bin/env python
from pathlib import Path
import json
import subprocess
import sys
import argparse

# ============================================================
# CONFIG
# ============================================================

FOLDERS_TO_SCAN = [
    r"C:\Temp_Media\TV Shows",
    r"C:\Temp_Media\Movies",
]

VERBOSE = True

ENCODER_PATTERNS = ["hls.js", "dailymotion"]

# Supported input containers
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov"}

# Codecs safe for remux into MP4
SAFE_VIDEO_CODECS = {"h264", "hevc", "mpeg4"}
SAFE_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "opus"}

# ============================================================


def run_ffprobe(path: Path):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return json.loads(result.stdout)
    except Exception as e:
        print(f"[ffprobe ERROR] {path}: {e}", file=sys.stderr)
        return None


def get_stream_codecs(ffinfo: dict):
    vcodec = None
    acodec = None

    for s in ffinfo.get("streams", []):
        if s.get("codec_type") == "video" and not vcodec:
            vcodec = s.get("codec_name")
        elif s.get("codec_type") == "audio" and not acodec:
            acodec = s.get("codec_name")

    return (vcodec or "").lower(), (acodec or "").lower()


def needs_fix(ffinfo: dict) -> bool:
    fmt = ffinfo.get("format", {})
    tags = fmt.get("tags", {}) or {}
    enc = (tags.get("encoder") or "").lower()

    if enc.startswith("lavf"):
        return False

    if any(p in enc for p in ENCODER_PATTERNS):
        return True

    for s in ffinfo.get("streams", []):
        tags = s.get("tags", {}) or {}
        enc = (tags.get("encoder") or "").lower()
        if any(p in enc for p in ENCODER_PATTERNS):
            return True

    return False


def remux_to_mp4(src: Path, dest: Path, dry_run: bool):
    if dry_run:
        print(f"[DRY RUN] Remux:\n  {src}\n  -> {dest}")
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-map", "0",
        "-c", "copy",
        "-movflags", "+faststart",
        str(dest),
    ]

    print(f"[FFMPEG] Remuxing:\n  {src}\n  -> {dest}")
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ffmpeg ERROR] {src}: {e}", file=sys.stderr)
        return False


def reencode_to_mp4(src: Path, dest: Path, dry_run: bool):
    if dry_run:
        print(f"[DRY RUN] Re-encode:\n  {src}\n  -> {dest}")
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Try QSV hardware encoding first (Intel Iris Xe), fall back to software
    qsv_cmd = [
        "ffmpeg", "-y",
        "-hwaccel", "qsv",
        "-hwaccel_output_format", "qsv",
        "-i", str(src),
        "-map", "0",
        "-c:v", "h264_qsv",
        "-global_quality", "23",
        "-c:a", "aac",
        "-c:s", "copy",
        "-movflags", "+faststart",
        str(dest),
    ]

    sw_cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-map", "0",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-c:a", "aac",
        "-c:s", "copy",
        "-movflags", "+faststart",
        str(dest),
    ]

    print(f"[FFMPEG] Re-encoding (QSV):\n  {src}\n  -> {dest}")
    try:
        subprocess.run(qsv_cmd, check=True)
        return True
    except subprocess.CalledProcessError:
        print(f"[QSV FAILED] Falling back to software encode: {src}")

    print(f"[FFMPEG] Re-encoding (software):\n  {src}\n  -> {dest}")
    try:
        subprocess.run(sw_cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ffmpeg ERROR] {src}: {e}", file=sys.stderr)
        return False


def process_file(path: Path, dry_run: bool):
    ffinfo = run_ffprobe(path)
    if not ffinfo:
        return

    ext = path.suffix.lower()
    vcodec, acodec = get_stream_codecs(ffinfo)

    final_mp4 = path.with_suffix(".mp4")
    tmp_out = final_mp4.with_suffix(".tmp.mp4")
    success = False

    # --- AVI / MOV handling ---
    if ext in (".avi", ".mov"):
        can_remux = vcodec in SAFE_VIDEO_CODECS and acodec in SAFE_AUDIO_CODECS

        label = ext.lstrip(".").upper()
        if can_remux:
            print(f"[{label} -> REMUX] {path} (v={vcodec}, a={acodec})")
            success = remux_to_mp4(path, tmp_out, dry_run)
        else:
            print(f"[{label} -> REENCODE] {path} (v={vcodec}, a={acodec})")
            success = reencode_to_mp4(path, tmp_out, dry_run)

    # --- MP4 handling only ---
    elif ext == ".mp4":
        if not needs_fix(ffinfo):
            if VERBOSE:
                print(f"[SKIP] {path} (already OK)")
            return

        success = remux_to_mp4(path, tmp_out, dry_run)

    # --- MKV handling (convert to MP4) ---
    elif ext == ".mkv":
        if not needs_fix(ffinfo):
            if VERBOSE:
                print(f"[SKIP] {path} (already OK)")
            return

        can_remux = vcodec in SAFE_VIDEO_CODECS and acodec in SAFE_AUDIO_CODECS

        if can_remux:
            print(f"[MKV -> REMUX] {path} (v={vcodec}, a={acodec})")
            success = remux_to_mp4(path, tmp_out, dry_run)
        else:
            print(f"[MKV -> REENCODE] {path} (v={vcodec}, a={acodec})")
            success = reencode_to_mp4(path, tmp_out, dry_run)

    if success and not dry_run:
        print(f"[DELETE] {path}")
        path.unlink()
        print(f"[RENAME] {tmp_out} -> {final_mp4}")
        tmp_out.rename(final_mp4)


def find_video_files(root: Path):
    if root.is_file():
        if root.suffix.lower() in VIDEO_EXTS:
            yield root
        return

    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            yield p


def main():
    parser = argparse.ArgumentParser(
        description="Fix video files for Jellyfin (QSV hardware encoding, multi-container)."
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    dry_run = not args.apply

    print("=== Jellyfin Video Fixer (QSV + multi-container) ===")
    print(f"DRY_RUN={dry_run}")
    print("===================================================\n")

    for root in map(Path, FOLDERS_TO_SCAN):
        if not root.exists():
            print(f"[WARN] Missing path: {root}")
            continue

        for video in find_video_files(root):
            process_file(video, dry_run)


if __name__ == "__main__":
    main()
