# Thunderhead

Personal tooling for managing and maintaining my **ThunderheadFlix** Jellyfin media server.

## Scripts (`scripts/`)

- **`master_jf_operations.py`** — Full pipeline: fix metadata, rename files, migrate (with dry-run approval)
- **`fix_metadata_for_jellyfin.py`** — Fix problematic encoder tags via ffmpeg remux/re-encode
- **`fix_file_names.py`** — Runner for TV + movie renaming scripts
- **`fix_tv_names.py`** / **`fix_movie_names.py`** — Rename media files using OMDb lookups
- **`migrate_files.py`** — Move processed media from staging to library drives
- **`download_youtube_jellyfin.py`** — Download YouTube videos via yt-dlp

## Browser Extension (`browser-extension/`)

Two-part system for capturing and downloading HLS streams:

- **`hls-capture/`** — Chrome/Vivaldi Manifest V3 extension that intercepts m3u8 URLs with auto-capture support
- **`hls-server/`** — Local Python server (port 9876) that downloads captured streams with Jellyfin-friendly naming

This repo is primarily designed for my local media structure and workflows.
