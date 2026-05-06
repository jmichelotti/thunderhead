# Thunderhead

Personal tooling for managing a Jellyfin media server ("ThunderheadFlix"). Four subsystems:

- **`scripts/`** — Python utilities for renaming, fixing metadata, migrating, and downloading media
- **`browser-extension/`** — Chrome/Vivaldi extension + local Python server for capturing and downloading HLS streams
- **`analytics/`** — FastAPI service (port 1201) exposing Jellyfin stats: live sessions, library counts, storage, and playback history via the Playback Reporting plugin
- **`wrapped/`** — Static frontend served by the analytics service at `/wrapped/`, showing per-user viewing stats, currently-watching shows, library overview, and missing episode detection

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
│   ├── bitrate_scan.py            lightweight ffprobe bitrate scan across all libraries
│   ├── extract_subtitles.py       copy+rename standalone .srt files into staging
│   ├── fix_show_year.py           fix misnamed show folder+files via IMDb ID lookup
│   ├── run_audit.bat              wrapper for nightly Task Scheduler job
│   └── audit_reports/             CSV reports, summary, issues .md, deep-decode cache (gitignored)
├── browser-extension/
│   ├── hls-capture/      background.js, content.js, popup.html, popup.js, manifest.json
│   ├── hls-server/       hls_download_server.py, read_server_log.py, *.bat files
│   └── auto-download/    auto_download_vivaldi.py — automated movie/show download via CDP
├── analytics/
│   ├── app.py                 FastAPI server (port 1201), all endpoints
│   ├── jellyfin_client.py     async Jellyfin API wrapper
│   ├── tvmaze_client.py       async TVmaze API wrapper (no auth needed)
│   ├── episode_gaps.py        detect missing episodes via TVmaze comparison
│   ├── config.py              server URL, API key, host/port (all env-overridable)
│   ├── Dockerfile             Python 3.12-slim container for the analytics service
│   ├── docker-compose.yml     container config: port 1201, restart unless-stopped
│   ├── tracked_shows.json     cached show→TVmaze ID mappings (gitignored)
│   └── requirements.txt       fastapi, uvicorn, httpx, tzdata
├── wrapped/
│   ├── index.html             dashboard shell (4 views: Wrapped, Watching, Library, Missing)
│   ├── styles.css             cinematic dark theme
│   └── app.js                 vanilla JS SPA fetching from analytics API
└── README.md
```

## Conventions

- All destructive scripts default to **dry-run**. Pass `--apply` to actually move/rename/download files.
- OMDb API key `591dfd18` is used across multiple scripts and the server for metadata lookups.
- `sanitize_for_windows()` is duplicated in several files — this is intentional, not a refactor target.
- External tools required: **ffmpeg/ffprobe**, **yt-dlp**.
- Python 3.10+. Scripts need `requests` (and optionally `langdetect`). Analytics needs `fastapi`, `uvicorn`, `httpx`.

## Key Paths

- **Staging area**: `C:\Temp_Media\` (TV Shows, Movies subdirs) — where scripts pick up and process files
- **Final libraries**: `D:\TV Shows`, `F:\TV Shows`, `L:\TV Shows`, `D:\Movies`, `F:\Movies`, `L:\Movies`
- **HLS temp**: `C:\Temp_Media\_hls_tmp\`

- **Jellyfin server**: `http://localhost:8096`, API key `388076d3d5c84671b9602ae56f73ac34` (named "thunderhead-analytics")
- **Analytics service**: `http://127.0.0.1:1201`, frontend at `/wrapped/` — runs in Docker container `thunderhead-analytics` (`docker compose up -d` from `analytics/`)
- **Playback Reporting DB**: `C:\ProgramData\Jellyfin\Server\data\playback_reporting.db` (plugin-managed, data since 2026-02-17)

Hardcoded paths throughout the repo are intentional (personal machine config). Don't refactor them into shared config unless asked.
