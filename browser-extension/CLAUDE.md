# Browser Extension — HLS Capture for Jellyfin

Three-part system: a Chrome/Vivaldi Manifest V3 extension captures HLS stream URLs, a local Python server downloads them with Jellyfin-friendly naming, and an automation script drives the browser end-to-end.

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

auto-download/   Automated download pipeline (Vivaldi + nodriver CDP)
  auto_download_vivaldi.py   Main script — search, navigate, play, capture, download
  auto_download.py           CloakBrowser approach (archived, blocked by embed detection)
  PROGRESS.md                Development log, bugs, test results
  screenshots/               Debug screenshots from each run (gitignored)
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
| `/subtitle-content` | POST | Receive raw subtitle text content (browser-fetched for BrocoFlix CDN) |
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

CDN blocks all non-browser clients (403). Current approach (Phase 3.2 — 100% segment completion):
- `runBrocoflixSwDownload()` fetches segments from background.js service worker
- `declarativeNetRequest` spoofs Origin/Referer headers at network stack level
- `onSendHeaders` with `"extraHeaders"` captures full browser header set including cookies/Sec-* headers
- Direct binary POST to server upload endpoints (no base64 overhead)
- Separate socket pool eliminates HTTP/2 GOAWAY issues
- Per-segment 429 backoff: on HTTP 429, retries up to 5× with exponential backoff (5s, 10s, 20s, 40s, 80s)
- Sequential fetching only — concurrent workers trigger aggressive CDN rate limiting
- If all retries exhausted, falls back to page reload (but 429 backoff typically recovers every segment)

See memory file `brocoflix-download-attempts.md` for full approach history and what's been tried.

### BrocoFlix Subtitles

Three-layer approach (CDN 403s server-side `requests.get()`):
1. **HLS manifest extraction**: `extractManifestSubtitles()` parses `#EXT-X-MEDIA:TYPE=SUBTITLES` from the master m3u8 during download, fetches English track content via service worker, relays to `/subtitle-content`
2. **Auto-CC click**: `enableBrocoflixSubtitles()` injects into the embed iframe BEFORE the download starts (download pauses/destroys the video player), clicks the CC button and selects English — triggers subtitle network requests. Player uses custom `<pjsdiv>` elements (PJS player): CC button is `#player_parent_control_showSubtitles`, English option is `.lang[data-subkey='eng']`
3. **Browser-side fetch**: `sendSubtitle()` detects BrocoFlix pages and fetches intercepted `.vtt`/`.srt` URLs in the service worker instead of sending the URL to the server

## Bat Files

All bat files have hardcoded path `C:\dev\thunderhead\browser-extension\hls-server\`. If the repo moves, update these files.

- `start_server.bat` — Opens new console, runs server with `--apply`
- `restart_server.bat` — Kills existing process (by name, command line, and port), restarts via `pythonw` (background)
- `stop_server.bat` — Kills server process via PowerShell WMI query
- `setup_server_task.bat` — Creates `schtasks` entry for auto-start at logon (requires admin)

## Auto-Download Pipeline

Fully automated download: search a streaming site, navigate to the result, start playback, capture the m3u8, confirm the download, and monitor until complete. Uses real Vivaldi browser via nodriver (CDP) — zero bot detection surface.

### Usage

```
# Movies (BrocoFlix, Server 2)
python auto_download_vivaldi.py "The Thing" 1982
python auto_download_vivaldi.py "Total Recall" 1990 "True Lies" 1994

# TV Shows (1movies, Server 1)
python auto_download_vivaldi.py --show "Daredevil Born Again" --season 1 --episode 1
python auto_download_vivaldi.py --show "Daredevil Born Again" --season 1 --episodes 2-9
python auto_download_vivaldi.py --show "Daredevil Born Again" --seasons 1-2
```

### Requirements

- Vivaldi browser with automation profile at `C:\Temp_Media\_vivaldi_automation`
- HLS capture extension loaded in the automation profile (one-time: `python auto_download_vivaldi.py --setup`)
- HLS server running (`start_server.bat`)
- Python: `nodriver`, `websockets`, `requests`

### How it works

- **Movies**: launches Vivaldi, searches BrocoFlix, clicks result, clicks Watch Now + Server 2, clicks iframe to play, confirms m3u8 capture via CDP websocket to extension service worker, monitors BrocoFlix browser-side upload until done
- **TV shows (single episode)**: navigates to 1movies, searches, clicks show, sets `location.hash` to correct episode, clicks play button, confirms m3u8 via service worker, yt-dlp downloads
- **TV shows (range/seasons)**: same navigation, then triggers extension auto-capture via `startAutoCapture` message to service worker, forces episode range after DOM discovery, monitors auto-capture state + yt-dlp downloads
- **Multi-movie**: each movie gets its own Vivaldi instance (separate CDP port + cloned profile), downloads in parallel

### Key pattern: `js()` helper

All JS evaluation uses `js(tab, expr)` which calls CDP `Runtime.evaluate` directly and recursively unwraps deep-serialized values. All JS must be IIFEs: `"(() => { ... })()"`. nodriver's `tab.evaluate()` has multiple bugs with return values.

See `auto-download/PROGRESS.md` for full development history, bugs fixed, and test results.
