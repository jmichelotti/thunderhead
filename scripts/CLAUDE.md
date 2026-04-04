# Scripts

Python utilities for renaming, fixing, migrating, and downloading media files for Jellyfin.

## Scripts

- **`master_jf_operations.py`** — Runs the full pipeline in order: fix metadata -> fix names -> migrate (dry-run preview with approve/deny prompt before applying).
- **`fix_file_names.py`** — Runner that calls `fix_tv_names.py` + `fix_movie_names.py` with `--apply`. Uses `Path(__file__).parent` to find sibling scripts.
- **`fix_tv_names.py`** — Parses `ShowName SxxExx` from filenames, looks up series metadata via OMDb, moves into `Show Name (Year)/Season XX/` structure. Supports combined episodes (`S06E20&21`), IMDb ID extraction from filenames, and `IMDB_TITLE_OVERRIDES` for manual corrections. Root: `C:\Temp_Media\TV Shows`.
- **`fix_movie_names.py`** — Lookup chain: IMDb ID -> OMDb (with IMDb suggestion API fallback) -> exact title -> strip year + retry -> split on `-` + search -> full search. Creates `Title (Year)/Title (Year).ext`. Root: `C:\Temp_Media\Movies`.
- **`fix_metadata_for_jellyfin.py`** — Fixes files with problematic encoder tags ("hls.js", "dailymotion"). Tries QSV hardware encoding first (Intel Iris Xe), falls back to software x264. Handles MP4/MKV/AVI/MOV. Scans both `C:\Temp_Media\TV Shows` and `C:\Temp_Media\Movies`.
- **`migrate_files.py`** — Moves processed media from `C:\Temp_Media\` to final library drives. TV routing: checks `D:\TV Shows` then `F:\TV Shows` for existing shows, new shows go to `L:\TV Shows`. Movies always go to `L:\Movies`. Handles file conflicts with `(migrated N)` suffix.
- **`download_youtube_jellyfin.py`** — Downloads YouTube videos as `Title (Year).mp4`. Uses `--extractor-args youtube:player_client=android` workaround. Output: `C:\Temp_Media\YouTube`.

## Conventions

- Every script that modifies files uses `--apply` (dry-run by default).
- Video extensions: `.mp4`, `.mkv`, `.avi`, `.mov`
- OMDb lookups use API key `591dfd18` with `requests` library.
