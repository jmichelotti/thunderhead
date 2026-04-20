# Browser Extension — HLS Capture for Jellyfin

Two-part system: a Chrome/Vivaldi Manifest V3 extension captures HLS stream URLs, and a local Python server downloads them with Jellyfin-friendly naming.

## Architecture

```
hls-capture/     Browser extension (MV3 service worker)
  background.js  Service worker — intercepts m3u8 requests, auto-capture orchestration, BrocoFlix SW download
  content.js     Content script — injected on all frames, DOM inspection, episode navigation, auto-capture coordination
  popup.html/js  Popup UI — pending captures, download progress, auto-capture controls, DOM inspector
  manifest.json  Permissions: webRequest, activeTab, storage, scripting, declarativeNetRequest

hls-server/      Local Python download server
  hls_download_server.py   HTTP server on port 9876, receives m3u8 URLs, downloads via yt-dlp
  read_server_log.py       Tail the server log file
  start_server.bat         Start server in new console window with --apply
  restart_server.bat       Kill existing server process and restart in background (pythonw)
  stop_server.bat          Kill server process
  setup_server_task.bat    Register Windows scheduled task for auto-start at logon (run as admin)
```

## Server

- **Port**: 9876 (configurable via `--port`)
- **Dry-run by default** — pass `--apply` for real downloads
- **Output**: `C:\Temp_Media\TV Shows\` (organized into show/season folders)
- **Temp dir**: `C:\Temp_Media\_hls_tmp\`
- **Log**: `hls_server.log` in the server directory, auto-rotates at 5 MB. Use `read_server_log.py` to tail.

### Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/capture` | POST | Receive m3u8 URL, trigger yt-dlp download |
| `/preview` | POST | Analyze m3u8, return filename/quality/show info (no download) |
| `/subtitle` | POST | Receive VTT subtitle URL, download and convert to SRT if English |
| `/season-info` | POST | Log discovered episode list at start of each season (auto-capture) |
| `/downloads` | GET | Active/completed download list with progress |
| `/status` | GET | Health check, returns `{"dry_run": bool}` |
| `/clear` | GET | Clear download history and seen URLs |
| `/upload-start` | POST | Begin browser-side HLS upload session (BrocoFlix) |
| `/upload-chunk` | POST | Append raw binary segment to session temp file |
| `/upload-done` | POST | Finalize upload session: close + move to output path |
| `/upload-error` | POST | Abort session: close + delete temp file |

### Show Name Lookup

OMDb lookup chain from streaming site URL slug (e.g. `tv-paradise4-4vdbe`):
1. Strip 5-char alphanumeric suffix
2. Try OMDb exact title match
3. Strip trailing digits and retry
4. Try OMDb fuzzy search
5. Fall back to raw slug name

### English Subtitle Detection

`is_english_subtitle()` returns `(bool, reason_str)`. Layers: ASCII ratio, CP1250 mojibake detection, accented character ratio, foreign word list, langdetect (if installed).

## Supported Sites

Configured in `SITE_CONFIGS` in `background.js`:

- **1movies** — `navStrategy: "hash-reload"`. URL pattern: `tv-show-name-xxxxx#ep=<season>,<episode>`. Auto-capture navigates by changing `location.hash` then reloading.
- **brocoflix.xyz** — `navStrategy: "click-card"`. Navigates by clicking `.episode-card` elements. Has `seasonSelectSelector` for season dropdown.

## Auto-Capture

Bulk-downloads a range of episodes unattended. Two modes:
- **Episodes**: Single season, episode range (e.g. S1 EP1-10)
- **Seasons**: Multi-season, auto-detects episode count per season via DOM scanning

Key design decisions:
- `waitForEpisodeDone` runs BEFORE sleep to catch m3u8 from auto-playing video
- `graceUntil` (15s) keeps auto-confirm running after last episode advance
- `epoch` + `episodeDoneSent` guards prevent duplicate/stale done-signals
- `episodeDoneSent` set immediately (not after 2s wait) to avoid race with content script polling
- `consecutiveSkips`: 2 skips = season done (handles unknown episode counts)
- DOM-based episode discovery: scans `a[href*="#ep="]` with poll-until-stable pattern
- `IS_TOP_FRAME` gate: the auto-capture loop and `chrome.runtime.onMessage` listener run only in the top frame. Manifest has `all_frames: true` so the BrocoFlix chunk relay can run in the video iframe; without the gate every frame would react to `autoCaptureEpisodeDone` and send its own `autoCaptureAdvance`, double-incrementing `currentEp` and skipping every other episode. `background.js` also rejects `autoCaptureAdvance` with `sender.frameId !== 0` as a safety net.

## BrocoFlix Special Handling

CDN blocks all non-browser clients (403). Current approach (Phase 3):
- `runBrocoflixSwDownload()` fetches segments from background.js service worker
- `declarativeNetRequest` spoofs Origin/Referer headers at network stack level
- Direct binary POST to server upload endpoints (no base64 overhead)
- Separate socket pool eliminates HTTP/2 GOAWAY issues
- Proactive reload disabled (`BROCOFLIX_RELOAD_THRESHOLD = 0`) — CDN no longer triggers GOAWAY
- If >=99.5% segments complete and retries stall, muxes with gaps rather than aborting
- MAIN-world catch routes errors through reload handler (not immediate abort) when segments exist

See memory file `brocoflix-download-attempts.md` for full approach history and what's been tried.

## Bat Files

All bat files have hardcoded path `C:\dev\thunderhead\browser-extension\hls-server\`. If the repo moves, update these files.

- `start_server.bat` — Opens new console, runs server with `--apply`
- `restart_server.bat` — Kills existing process (by name, command line, and port), restarts via `pythonw` (background)
- `stop_server.bat` — Kills server process via PowerShell WMI query
- `setup_server_task.bat` — Creates `schtasks` entry for auto-start at logon (requires admin)
