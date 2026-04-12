# Thunderhead

Personal tooling for managing a Jellyfin media server ("ThunderheadFlix"). Two main subsystems:

- **`scripts/`** — Python utilities for renaming, fixing metadata, migrating, and downloading media
- **`browser-extension/`** — Chrome/Vivaldi extension + local Python server for capturing and downloading HLS streams

## Repo Structure

```
thunderhead/
├── scripts/
│   ├── master_jf_operations.py   full pipeline: fix metadata -> fix names -> migrate
│   ├── fix_tv_names.py
│   ├── fix_movie_names.py
│   ├── fix_file_names.py
│   ├── fix_metadata_for_jellyfin.py
│   ├── migrate_files.py
│   ├── download_youtube_jellyfin.py
│   ├── shift_subtitles.py
│   ├── audit_jellyfin.py          3-tier library audit (structural, layout, decode)
│   ├── run_audit.bat              wrapper for nightly Task Scheduler job
│   └── audit_reports/             CSV reports, summary, deep-decode cache (gitignored)
├── browser-extension/
│   ├── hls-capture/   background.js, content.js, popup.html, popup.js, manifest.json
│   └── hls-server/    hls_download_server.py, read_server_log.py, *.bat files
└── README.md
```

## Conventions

- All destructive scripts default to **dry-run**. Pass `--apply` to actually move/rename/download files.
- OMDb API key `591dfd18` is used across multiple scripts and the server for metadata lookups.
- `sanitize_for_windows()` is duplicated in several files — this is intentional, not a refactor target.
- External tools required: **ffmpeg/ffprobe**, **yt-dlp**.
- Python 3.10+. Only pip dependencies are `requests` and optionally `langdetect`.

## Key Paths

- **Staging area**: `C:\Temp_Media\` (TV Shows, Movies subdirs) — where scripts pick up and process files
- **Final libraries**: `D:\TV Shows`, `F:\TV Shows`, `L:\TV Shows`, `D:\Movies`, `F:\Movies`, `L:\Movies`
- **HLS temp**: `C:\Temp_Media\_hls_tmp\`

Hardcoded paths throughout the repo are intentional (personal machine config). Don't refactor them into shared config unless asked.
