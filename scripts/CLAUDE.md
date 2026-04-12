# Scripts

Python utilities for renaming, fixing, migrating, and downloading media files for Jellyfin.

## Scripts

- **`master_jf_operations.py`** — Runs the full pipeline in order: fix metadata -> fix names -> migrate (dry-run preview with approve/deny prompt before applying).
- **`fix_file_names.py`** — Runner that calls `fix_tv_names.py` + `fix_movie_names.py` with `--apply`. Uses `Path(__file__).parent` to find sibling scripts.
- **`fix_tv_names.py`** — Parses `ShowName SxxExx` from filenames, looks up series metadata via OMDb, moves into `Show Name (Year)/Season XX/` structure. Supports combined episodes (`S06E20&21`), IMDb ID extraction from filenames, and `IMDB_TITLE_OVERRIDES` for manual corrections. Root: `C:\Temp_Media\TV Shows`.
- **`fix_movie_names.py`** — Lookup chain: IMDb ID -> OMDb (with IMDb suggestion API fallback) -> exact title -> strip year + retry -> split on `-` + search -> full search. Creates `Title (Year)/Title (Year).ext`. Root: `C:\Temp_Media\Movies`.
- **`fix_metadata_for_jellyfin.py`** — Fixes files with problematic encoder tags ("hls.js", "dailymotion") at both format and stream level. Tries QSV hardware encoding first (Intel Iris Xe), falls back to software x264. Handles MP4/MKV/AVI/MOV. Defaults to `C:\Temp_Media\{TV Shows,Movies}`, but `--root` can target library drives directly (e.g. `--root "D:\TV Shows"`).
- **`migrate_files.py`** — Moves processed media from `C:\Temp_Media\` to final library drives. TV routing: checks `D:\TV Shows` then `F:\TV Shows` for existing shows, new shows go to `L:\TV Shows`. Movies always go to `L:\Movies`. Handles file conflicts with `(migrated N)` suffix.
- **`download_youtube_jellyfin.py`** — Downloads YouTube videos as `Title (Year).mp4`. Uses `--extractor-args youtube:player_client=android` workaround. Output: `C:\Temp_Media\YouTube`.
- **`shift_subtitles.py`** — Shifts .srt subtitle timestamps by a given number of seconds. Accepts a file path directly or `--scan` to find .srt files in staging dirs. Positive values shift forward, negative shift backward. Timestamps floor at `00:00:00,000`.
- **`audit_jellyfin.py`** — 3-tier audit of final library drives (`{D,F,L}:\{TV Shows,Movies}`). Tier 1: ffprobe structural checks (streams, duration, codecs, encoder tags, container). Tier 2: naming/layout validation against `Show (Year)/Season NN/SxxExx` and `Title (Year)/Title (Year).ext` conventions. Tier 3 (`--deep`): full ffmpeg decode sweep, cache-gated on `(size, mtime)` so repeat runs only re-check new/changed files. `LAYOUT_WHITELIST` exempts shows like P90X from layout checks. Outputs CSV + summary to `audit_reports/`.
- **`run_audit.bat`** — Wrapper for nightly `--deep` audit via Windows Task Scheduler (2:00 AM). Logs to `audit_reports/nightly.log`.

## Conventions

- Every script that modifies files uses `--apply` (dry-run by default).
- Video extensions: `.mp4`, `.mkv`, `.avi`, `.mov`
- OMDb lookups use API key `591dfd18` with `requests` library.
