#!/usr/bin/env python
"""Read recent output from the HLS download server log.

Usage:
    python read_server_log.py                # last 30 lines (default)
    python read_server_log.py --lines 50     # last 50 lines
    python read_server_log.py --chars 500    # last 500 characters
    python read_server_log.py --all          # entire log
"""

import argparse
import sys
from pathlib import Path

LOG_FILE = Path(__file__).parent / "hls_server.log"


def tail_lines(path: Path, n: int) -> str:
    """Return the last n lines of a file efficiently."""
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"Log file not found: {path}"
    lines = data.splitlines(keepends=True)
    return "".join(lines[-n:])


def tail_chars(path: Path, n: int) -> str:
    """Return the last n characters of a file."""
    try:
        size = path.stat().st_size
        read_bytes = min(size, n + 256)  # extra for partial UTF-8 chars
        with open(path, "rb") as f:
            f.seek(max(0, size - read_bytes))
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        return text[-n:]
    except FileNotFoundError:
        return f"Log file not found: {path}"


def main():
    parser = argparse.ArgumentParser(description="Read HLS server log")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--lines", "-n", type=int, default=30,
                       help="Number of lines to show (default: 30)")
    group.add_argument("--chars", "-c", type=int,
                       help="Number of characters to show")
    group.add_argument("--all", "-a", action="store_true",
                       help="Show entire log")
    args = parser.parse_args()

    if not LOG_FILE.exists():
        print(f"No log file found at {LOG_FILE}")
        print("The server hasn't been started yet.")
        sys.exit(1)

    if args.all:
        print(LOG_FILE.read_text(encoding="utf-8", errors="replace"), end="")
    elif args.chars:
        print(tail_chars(LOG_FILE, args.chars), end="")
    else:
        print(tail_lines(LOG_FILE, args.lines), end="")


if __name__ == "__main__":
    main()
