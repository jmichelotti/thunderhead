# Scripts

Python utilities for renaming, fixing, migrating, and downloading media files for Jellyfin.

## Scripts

- **`master_jf_operations.py`** — Runs the full pipeline in order: fix metadata -> fix names -> migrate (dry-run preview with approve/deny prompt before applying).
- **`fix_file_names.py`** — Runner that calls `fix_tv_names.py` + `fix_movie_names.py` with `--apply`. Uses `Path(__file__).parent` to find sibling scripts.
- **`fix_tv_names.py`** — Parses `ShowName SxxExx` from filenames, looks up series metadata via OMDb, moves into `Show Name (Year)/Season XX/` structure. Supports combined episodes (`S06E20&21`), IMDb ID extraction from filenames, and `IMDB_TITLE_OVERRIDES` for manual corrections. Root: `C:\Temp_Media\TV Shows`.
- **`fix_movie_names.py`** — Lookup chain: IMDb ID -> OMDb (with IMDb suggestion API fallback) -> exact title -> strip year + retry -> split on `-` + search -> full search. Creates `Title (Year)/Title (Year).ext`. Root: `C:\Temp_Media\Movies`.
- **`fix_metadata_for_jellyfin.py`** — Fixes files with problematic encoder tags ("hls.js", "dailymotion") at both format and stream level. Tries QSV hardware encoding first (Intel Iris Xe), falls back to software x264. Handles MP4/MKV/AVI/MOV. Defaults to `C:\Temp_Media\{TV Shows,Movies}`, but `--root` can target library drives directly (e.g. `--root "D:\TV Shows"`).
- **`migrate_files.py`** — Moves processed media from `C:\Temp_Media\` to final library drives. TV routing: checks `D:\TV Shows` then `F:\TV Shows` for existing shows, new shows go to `L:\TV Shows`. Movies always go to `L:\Movies`. Handles file conflicts with `(migrated N)` suffix. `--replace` overwrites existing files at destination (for replacing bad files with redownloaded copies).
- **`download_youtube_jellyfin.py`** — Downloads YouTube videos as `Title (Year).mp4`. Uses `--extractor-args youtube:player_client=android` workaround. Output: `C:\Temp_Media\YouTube`.
- **`shift_subtitles.py`** — Shifts .srt subtitle timestamps by a given number of seconds. Accepts a file path directly or `--scan` to find .srt files in staging dirs. Positive values shift forward, negative shift backward. Timestamps floor at `00:00:00,000`.
- **`audit_jellyfin.py`** — 3-tier audit of final library drives (`{D,F,L}:\{TV Shows,Movies}`). Tier 1: ffprobe structural checks (streams, duration, codecs, encoder tags, container). Tier 2: naming/layout validation against `Show (Year)/Season NN/SxxExx` and `Title (Year)/Title (Year).ext` conventions. Tier 3 (`--deep`): full ffmpeg decode sweep, cache-gated on `(size, mtime)` so repeat runs only re-check new/changed files. `LAYOUT_WHITELIST` exempts shows like P90X from layout checks. Outputs CSV + summary + prioritized `.md` issues report to `audit_reports/`. **Read-only with respect to media**: ffmpeg runs with `-f null -` and a runtime assertion rejects any command that doesn't match; per-file `(size, mtime)` snapshot before/after decode emits `file_modified_during_decode` if either changes. `--cpu-limit N` wraps tier-3 ffmpeg in a Windows Job Object with `JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP` so ffmpeg auto-threads across all cores at ≤N% total CPU (quiet fan); at `--cpu-limit 0` it falls back to `-threads 1`; `--no-limit` runs at full CPU with auto-threads. PID lockfile at `audit_reports/.audit.lock` (stale-cleanup via `OpenProcess` liveness check) prevents duplicate-instance runs; SIGINT/SIGTERM save cache and release the lock. Cache writes use atomic temp-file + read-back verification + retry on Windows file lock contention.
- **`bitrate_scan.py`** — Lightweight ffprobe-only bitrate scan of all Jellyfin libraries. Runs at below-normal priority so it won't interfere with other work. Outputs CSV (`audit_reports/bitrate_scan.csv`) and summary with per-show avg/min/max bitrate, flags low bitrate files (redownload candidates) and high bitrate files (re-encode candidates). `--low`/`--high` thresholds configurable, `--drive` to limit scope.
- **`run_audit.bat`** — Wrapper for nightly `--deep --cpu-limit 25` audit via Windows Task Scheduler (2:00 AM). Launches under `start /B /BELOWNORMAL /WAIT` for below-normal priority. Logs to `audit_reports/nightly.log`. The script-level PID lockfile is a backstop if Task Scheduler ever fires overlapping runs.

## Conventions

- Every script that modifies files uses `--apply` (dry-run by default).
- Video extensions: `.mp4`, `.mkv`, `.avi`, `.mov`
- OMDb lookups use API key `591dfd18` with `requests` library.
