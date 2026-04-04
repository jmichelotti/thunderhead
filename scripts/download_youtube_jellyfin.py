import subprocess
import sys
from pathlib import Path

OUTPUT_DIR = Path(r"C:\Temp_Media\YouTube")

def download_for_jellyfin(url: str):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        "yt-dlp",

        "--extractor-args", "youtube:player_client=android",

        "-f", "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b",

        "--merge-output-format", "mp4",
        "--postprocessor-args", "ffmpeg:-movflags +faststart",

        "--no-continue",
        "--retries", "10",
        "--fragment-retries", "10",

        "-o", str(OUTPUT_DIR / "%(title)s (%(upload_date>%Y)s).mp4"),

        url
    ]

    subprocess.run(cmd, check=True)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python download_youtube_jellyfin.py <URL>")
        sys.exit(1)

    download_for_jellyfin(sys.argv[1])
