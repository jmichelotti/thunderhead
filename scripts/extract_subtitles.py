"""Extract standalone subtitle files from downloaded show folders into TV Shows staging."""

import argparse
import shutil
from pathlib import Path

OUTPUT_DIR = Path(r"C:\Temp_Media\TV Shows")


def find_subtitles_season_episode_dirs(source: Path):
    """Pattern: Season X/Episode Y/*.srt"""
    results = []
    for season_dir in sorted(source.iterdir()):
        if not season_dir.is_dir():
            continue
        m = _parse_season_number(season_dir.name)
        if m is None:
            continue
        season_num = m
        for ep_dir in sorted(season_dir.iterdir()):
            if not ep_dir.is_dir():
                continue
            ep_num = _parse_episode_number(ep_dir.name)
            if ep_num is None:
                continue
            for srt in ep_dir.glob("*.srt"):
                results.append((season_num, ep_num, srt))
    return results


PATTERNS = [
    ("Season X/Episode Y dirs", find_subtitles_season_episode_dirs),
]


def _parse_season_number(name: str) -> int | None:
    name_lower = name.lower().strip()
    if name_lower.startswith("season "):
        try:
            return int(name_lower.removeprefix("season ").strip())
        except ValueError:
            return None
    return None


def _parse_episode_number(name: str) -> int | None:
    name_lower = name.lower().strip()
    if name_lower.startswith("episode "):
        try:
            return int(name_lower.removeprefix("episode ").split()[0].strip(" -"))
        except ValueError:
            return None
    return None


def main():
    parser = argparse.ArgumentParser(description="Extract subtitle files into TV Shows staging")
    parser.add_argument("source", help="Path to the downloaded show folder")
    parser.add_argument("name", help='Show name for output files (e.g. "John Adams")')
    parser.add_argument("--apply", action="store_true", help="Actually copy files (default is dry-run)")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_dir():
        print(f"Error: {source} is not a directory")
        return

    subtitles = []
    for pattern_name, finder in PATTERNS:
        subtitles = finder(source)
        if subtitles:
            print(f"Detected pattern: {pattern_name}")
            break

    if not subtitles:
        print("No subtitle files found matching any known pattern.")
        return

    print(f"Found {len(subtitles)} subtitle(s):\n")
    for season, episode, srt_path in subtitles:
        out_name = f"{args.name} S{season}E{episode}.srt"
        out_path = OUTPUT_DIR / out_name
        print(f"  {srt_path.name}")
        print(f"    -> {out_path}")

        if args.apply:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srt_path, out_path)

    print()
    if args.apply:
        print(f"Copied {len(subtitles)} file(s) to {OUTPUT_DIR}")
    else:
        print("DRY RUN — pass --apply to copy files")


if __name__ == "__main__":
    main()
