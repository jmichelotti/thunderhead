# Auto-Download: Automated Browser Extension Download Pipeline

## Goal

Take input like `"The Thing" 1982` and automatically:
1. Launch a stealth browser with the HLS capture extension loaded
2. Navigate to a streaming site (BrocoFlix, 1movies)
3. Search for the movie/show
4. Click the correct result
5. Start playback (select server, click play)
6. Extension captures m3u8 stream
7. Extension sends to HLS server on :9876 for download

## Machine Environment

| Tool | Version | Notes |
|------|---------|-------|
| **CloakBrowser** | 0.3.25 | Stealth Chromium 145, 49 C++ anti-detect patches, Playwright API |
| **nodriver** | 0.48.1 | CDP websocket automation, successor to undetected-chromedriver |
| **camoufox** | 0.4.11 | Firefox-only, can't load Chrome MV3 extensions — eliminated |
| **playwright** | 1.57.0 | Chrome 147 killed `--load-extension` — broken for extensions |
| **rebrowser-playwright** | 1.52.0 | Fixes Runtime.enable leak, same extension issue |
| **selenium** | 4.40.0 | Same Chrome 147 extension issue — eliminated |
| **browserforge** | 1.2.4 | Fingerprint generation |
| Chrome | 147.0.7727.138 | `--load-extension` removed in Chrome 137+ (branded builds) |
| Vivaldi | 7.9.3970.59 | Chromium fork, `--load-extension` still works |
| Edge | 147.0.3912.98 | Same as Chrome — extension loading broken |
| Python | 3.14.2 | |
| Node.js | 24.12.0 | |

## Approach 1: CloakBrowser (stealth Chromium 145)

### How it works
- Ships its own unbranded Chromium 145 binary (`--load-extension` still works)
- 49 source-level C++ patches: canvas, WebGL, audio, fonts, GPU, timing, etc.
- `humanize=True` adds Bezier mouse movements, natural typing, realistic scroll
- Full Playwright Python API (sync + async)
- Persistent profile via `launch_persistent_context()`

### Results

| Step | Status | Notes |
|------|--------|-------|
| Launch browser with extension | WORKING | Extension loads via `--load-extension` flag |
| Navigate to BrocoFlix | WORKING | No bot detection on the main site |
| Search for movie | WORKING | Progressive query simplification (e.g. "E.T. the Extra-Terrestrial" -> "E.T.") |
| Detect search results | WORKING (after fixes) | Had to use card-count heuristic, not link-with-image check |
| Score and rank results | WORKING | Year match + title word matching, filters nav/footer links |
| Click correct result | WORKING | Navigated to `brocoflix.xyz/pages/info?id=601&type=movie` for E.T. |
| Click Watch Now | WORKING | Found and clicked the button |
| Select Server 2 | WORKING | Clicked Server 2 button |
| Video playback in iframe | **BLOCKED** | vidsrc.xyz embed detected CloakBrowser and refused to serve video |
| m3u8 capture | **FAILED** | `seenM3u8: []` — no m3u8 ever requested because video never loaded |

### Key finding
BrocoFlix itself doesn't detect CloakBrowser, but the **embed provider** (vidsrc.xyz) does. The video iframe loads but stays black — no video, no m3u8 requests. CloakBrowser's Chromium 145 is likely flagged by version fingerprint.

### Script
`auto_download.py` — CloakBrowser-based approach (fully working through step 6, blocked at video embed)

## Approach 2: Vivaldi + nodriver (real browser via CDP) — WORKING

### How it works
- Launch real Vivaldi browser with `--remote-debugging-port=9222`
- Load extensions via `--load-extension=path1,path2`
- Connect nodriver via CDP websocket to control the browser
- Uses the REAL browser with real fingerprints — zero bot detection surface
- nodriver API: `await tab.get()`, `await tab.find()`, `await tab.select()`, `element.click()`, `element.send_keys()`
- Auto-confirm captures by connecting to extension service worker via CDP websocket

### Results

| Step | Status | Notes |
|------|--------|-------|
| Launch Vivaldi with extensions | WORKING | Both HLS capture and uBlock Origin load |
| Connect nodriver via CDP | WORKING | Connects to `localhost:9222` |
| Navigate to BrocoFlix | WORKING | Page loads fine |
| Search for movie | WORKING | Types query, submits via JS keyboard events |
| Detect search results | WORKING | Fixed — was nodriver IIFE + return_by_value bug |
| Score and click result | WORKING | Year match + title word scoring, filters nav links |
| Click Watch Now + Server | WORKING | Finds and clicks Watch Now, selects Server 2 |
| Video playback | WORKING | Real Vivaldi bypasses vidsrc.xyz bot detection |
| m3u8 capture | WORKING | Extension intercepts m3u8 automatically |
| Auto-confirm download | WORKING | CDP websocket to service worker, calls `confirmDownload(0)` |
| Monitor download | WORKING | Polls `/downloads` endpoint until `status == "done"` |
| Full movie download | WORKING | Tested: The Thing (1982), 1302 segments, 1030 MB, ~8 min |

### Bugs fixed (2026-05-06)

**1. nodriver `tab.evaluate()` doesn't auto-invoke arrow functions**
`Runtime.evaluate` treats `"() => { ... }"` as a function expression, not a call. It creates a function object but never executes it, returning a RemoteObject instead of the boolean result. Fix: wrap all arrow functions as IIFEs — `"(() => { ... })()"`.

**2. nodriver deep serialization returns nested CDP format**
`evaluate()` with `return_by_value=False` (default) uses `deep_serialized_value` which returns objects as `{'type': 'object', 'value': [['key', {'type': 'string', 'value': '...'}], ...]}` instead of plain dicts. Fix: custom `js()` helper that calls CDP `Runtime.evaluate` directly, preserves the `DeepSerializedValue.type_` info, and recursively unwraps via `_unwrap()`.

**3. nodriver `return_by_value=True` broken for falsy values**
The check `if remote_object.value:` on line 842 of nodriver's tab.py fails for `0`, `False`, empty lists — falls through to returning raw `RemoteObject`. Bypassed by using our own `js()` helper with deep serialization instead.

**4. Result scoring picked container elements over cards**
Parent elements containing multiple cards scored higher (more matching text). Fix: penalize elements with text > 80 chars, boost links to `/pages/info`, filter homepage links by TLD.

**5. `/downloads` endpoint returns `{"downloads": [...]}` not `[...]`**
The script was iterating over dict keys instead of the downloads array. Fix: `data.get("downloads", data)`.

**6. BrocoFlix uploads use status `"uploading"` not `"downloading"`**
`monitor_downloads` only checked for `downloading`/`queued` statuses. Fix: also check `uploading`, `muxing`, `moving`.

**7. Extension auto-confirm: wrong extension ID**
First attempt found uBlock Origin's ID (`cjpalhdlnbpafiamejdnhcphjbkeiagm`) instead of the HLS capture extension. Fix: explicitly skip uBlock's known ID when scanning CDP targets.

**8. Extension auto-confirm: popup-as-tab approach unreliable**
Opening `chrome-extension://<id>/popup.html` as a tab didn't reliably trigger the popup JS. Fix: connect directly to the service worker's CDP websocket target, call `confirmDownload(0)` via `Runtime.evaluate`.

### Script
`auto_download_vivaldi.py` — Vivaldi + nodriver approach (fully working end-to-end)

## BrocoFlix Site Structure

### URL patterns
- Homepage: `https://brocoflix.xyz/`
- Search: `https://brocoflix.xyz/pages/search?query=The%20Thing`
- Movie page: `https://brocoflix.xyz/pages/info?id=601&type=movie`
- Embed: `https://vidsrc.xyz/embed/movie?tmdb=1091`

### DOM structure
- Search input: `input[placeholder*='earch' i]`
- Result cards: `[class*="card"]` divs (NOT `<a>` tags with images)
- Server buttons: `text=Server 1`, `text=Server 2`, etc. (Server 2 preferred)
- Watch button: `text=Watch Now`
- Video iframe: `iframe[src*="vidsrc.xyz"]`
- Season dropdown: `#season-select` (TV shows)
- Episode cards: `.episode-card` (TV shows)

### Search behavior
- Full title searches often fail (too specific)
- Short queries work best: "E.T." finds the movie, "The Thing" finds the movie
- Year is NOT part of the search — used only to score/filter results
- "Search Results" heading + "Results for '...'" text appears when results exist
- "No results found. Try a different search term." appears when empty

### Scam ads
- Fake "Install the Update" dialog appears on the site
- uBlock Origin blocks it when loaded as an extension
- uBlock path: `C:\Users\thunderhead\AppData\Local\Vivaldi\User Data\Default\Extensions\cjpalhdlnbpafiamejdnhcphjbkeiagm\1.70.0_0`

## Extension Integration

### Auto-confirm via CDP service worker (working approach)
1. Query `http://localhost:9222/json` to find CDP targets
2. Skip uBlock Origin's known ID (`cjpalhdlnbpafiamejdnhcphjbkeiagm`)
3. Find the HLS capture extension's service worker target and its `webSocketDebuggerUrl`
4. Connect via `websockets` library to the service worker's CDP websocket
5. `Runtime.evaluate` → `pendingCaptures.length` to check for pending captures
6. `Runtime.evaluate` → `confirmDownload(0).then(() => 'confirmed')` with `awaitPromise: true`

### Approaches that didn't work
- **Extension popup as tab**: Opening `chrome-extension://<id>/popup.html` as a regular tab — popup JS loads but `chrome.runtime.sendMessage` sometimes fails silently
- **`tab.find("Download")` in popup tab**: DOM detection was unreliable, timing issues

### BrocoFlix download flow
On BrocoFlix, confirming a capture triggers `runBrocoflixSwDownload()` in the service worker (not a direct yt-dlp download) because the CDN blocks non-browser requests. The service worker fetches each HLS segment sequentially and POSTs raw binary to `/upload-start`, `/upload-chunk`, `/upload-done` on the HLS server. These uploads appear on the `/downloads` endpoint with `status: "uploading"`. After all segments are received, the server muxes TS→MP4 (`status: "muxing"`) and moves the file (`status: "moving"` → `"done"`).

## Search Query Builder

Progressive simplification algorithm:
```
Input: "E.T. the Extra-Terrestrial" (1982)
Queries: ["E.T.", "ET", "E.T. the", "E.T. the Extra-Terrestrial", "ET the Extra-Terrestrial"]

Input: "The Thing" (1982)  
Queries: ["The Thing"]  (single word "The" filtered as stop word)

Input: "The Lord of the Rings: The Fellowship of the Ring" (2001)
Queries: ["The Lord of the Rings", "The Lord", "The Lord of the Rings: The Fellowship of the Ring"]
```

Stop words filtered from first-word-only queries: the, a, an, of, and, in, on, at, to, for, is

## Multi-Movie Support

### How it works
Each movie gets its own Vivaldi browser instance with a unique CDP port and cloned profile:
- Movie 1: port 9222, base profile `_vivaldi_automation`
- Movie 2: port 9223, cloned profile `_vivaldi_automation_2`
- Movie N: port 9222+N-1, cloned profile `_vivaldi_automation_N`

### CLI
```
python auto_download_vivaldi.py "Total Recall" 1990 "True Lies" 1994
python auto_download_vivaldi.py "Movie1" YEAR1 "Movie2" YEAR2 "Movie3" YEAR3
```

### Sequence
1. `prepare_profiles()` clones base profile for each additional instance (before any browser launches — avoids Windows file locks)
2. All Vivaldi instances launched simultaneously via `subprocess.Popen`
3. 6-second wait for browsers to initialize
4. Download tasks started with 15-second stagger (avoids simultaneous vidsrc.xyz hits)
5. Each task independently: search → click → play → confirm via service worker
6. Single `monitor_downloads()` watches all active downloads with multi-movie progress display
7. Cloned profiles cleaned up after completion

### Bugs fixed (multi-movie)

**9. Cross-movie false positive on HLS server**
`wait_for_capture_and_confirm` checked the shared HLS server's `/downloads` — if Movie A was downloading, Movie B would see "1 active" and falsely report success. Fix: only trust the service worker for confirmation (it's per-browser-instance).

**10. Cloned profile missing extensions (Windows file locks)**
`shutil.copytree` of the base profile failed silently when the first Vivaldi instance had files locked. Fix: clone ALL profiles before launching ANY browser.

**11. Stale cloned profiles from previous runs**
If a run was interrupted, leftover `_vivaldi_automation_N` directories had bad state. Fix: always delete and re-clone, never reuse existing cloned profiles.

**12. CDP error spam on failed connections**
Service worker websocket errors printed 11+ identical lines during the retry loop. Fix: quiet mode after first error — only print once, then retry silently.

### Tested
- Total Recall (1990) + True Lies (1994) — Total Recall downloaded (2014 MB), True Lies failed due to transient vidsrc.xyz DNS outage (not automation-related)
- Beverly Hills Cop (1984) — single movie, worked perfectly
- Lethal Weapon (1987) — single movie, worked perfectly

### Known limitation
vidsrc.xyz (BrocoFlix's embed provider) occasionally has DNS outages. When this happens, the video iframe shows "server IP address could not be found" and no m3u8 is captured. This is NOT caused by automation — the same failure occurs in manual browsing. Retry later when vidsrc.xyz is back up.

## TV Show Support (1movies) — WORKING

### How it works
- Launches Vivaldi, navigates to 1moviesz.to
- Searches for the show, clicks the correct result
- Navigates to the correct episode via `location.hash = '#ep=season,episode'` + reload
- Clicks the play button (`#player button.player-btn`) to start video
- Single episodes: confirms m3u8 via service worker (same as movie flow)
- Multi-episode: triggers extension auto-capture via CDP, monitors progress via `getAutoCaptureState`
- Downloads go through yt-dlp (not browser-side upload like BrocoFlix)

### CLI
```
python auto_download_vivaldi.py --show "Show Name" --season 1 --episode 5
python auto_download_vivaldi.py --show "Show Name" --season 1 --episodes 2-9
python auto_download_vivaldi.py --show "Show Name" --seasons 1-3
```

### Auto-capture integration
The script sends `startAutoCapture` to the extension's service worker via CDP websocket. Two modes:
- **Episodes mode**: `{season, startEp, endEp, serverNum}` — extension handles hash-reload navigation per episode
- **Seasons mode**: `{multiSeason: true, startSeason, endSeason, serverNum}` — extension auto-detects episode count per season

After starting auto-capture, `force_auto_capture_range()` overrides `endEp` back to the user's requested value (the extension's DOM discovery overrides it to the total episode count on the page).

Progress is monitored via `get_auto_capture_state()` which queries auto-capture state from the service worker.

### Bugs fixed (TV shows)

**13. Search result detection failed on 1movies**
The `has_results` check only looked for BrocoFlix-specific text ("Search Results"). Fix: also detect 1movies structure (`.film_list-wrap`, `.flw-item`, `[class*="film"]` elements) and fall back to URL keyword param + image count.

**14. Extension content.js didn't match 1moviesz.to**
`getCurrentSite()` checked for `"1movies.bz"` (old domain). Fix: changed to `host.includes("1movies")` to match any 1movies domain.

**15. Show page opens on latest episode, not EP1**
Clicking a TV show result lands on the most recent episode (e.g. `#ep=1,9`). Fix: after clicking, set `location.hash = '#ep=season,startEp'` and reload.

**16. Video doesn't auto-play on 1movies**
The player shows a play button overlay. Fix: click `#player button.player-btn` after navigating to the episode, with fallback to CDP mouse click on player center.

**17. Episode discovery overrides user-specified endEp**
The extension's `autoCaptureEpisodesDiscovered` handler sets `endEp = episodeHashes.length`, overriding the user's range. Fix: `force_auto_capture_range()` waits 8s for discovery, then forces `endEp` back via the service worker. Script also tracks `expected_count` independently.

### Tested
- Daredevil: Born Again S01E01 — single episode, 523 MB, 57 seconds
- Daredevil: Born Again S01 EP2-9 — episode range, 7/8 succeeded (1 transient failure)
- Daredevil: Born Again S02 (--seasons 2-2) — full season auto-detect, 8/8 succeeded, ~5 min total

### Known issues
- 1movies search result text shows as empty in the script output (DOM structure differs from BrocoFlix) — cosmetic only, correct result is still clicked
- Embed providers (rapidshareee.site, vidsrc.xyz) can IP-ban after rapid automated requests — use Cloudflare WARP to get a fresh IP if blocked

## Next Steps

1. ~~Fix `has_results()` in Vivaldi/nodriver~~ — DONE
2. ~~Test video playback with Vivaldi~~ — DONE
3. ~~Test capture confirmation~~ — DONE
4. ~~Test full download~~ — DONE
5. ~~Add multi-movie support~~ — DONE (parallel browser instances)
6. ~~Add 1movies support~~ — DONE (TV show search, navigation, auto-capture)
7. ~~TV show single episode~~ — DONE (confirms via service worker, no auto-capture)
8. ~~TV show episode range~~ — DONE (auto-capture with forced endEp)
9. ~~TV show full season~~ — DONE (multi-season auto-detect)
10. **Fix OMDb title lookup** — extension shows "The Thing (2020)" instead of 1982
11. **Handle duplicate pending captures** — service worker sometimes has 2 pending captures
12. **Error recovery** — retry on transient failures, re-download failed episodes
13. **Fix 1movies result text extraction** — cards show empty text in output

## Key Implementation Details

### `js()` helper — safe evaluate wrapper
All JS evaluation goes through `js(tab, expression)` which:
1. Calls CDP `Runtime.evaluate` directly (bypasses nodriver's broken extraction)
2. Preserves `DeepSerializedValue.type_` for correct object vs array unwrapping
3. Recursively unwraps via `_unwrap()` to plain Python dicts/lists/scalars
4. All JS must be IIFEs: `"(() => { ... })()"` — bare arrow functions return function objects

### `element.apply()` is different
nodriver's `element.apply("(el) => { ... }")` uses `Runtime.callFunctionOn` which DOES auto-invoke the function with the element as argument. These do NOT need the IIFE wrapper.

## Dependencies

```
nodriver==0.48.1    # CDP browser automation
websockets==16.0    # CDP websocket for service worker communication
requests            # HLS server API calls
```

## File Locations

- `auto_download.py` — CloakBrowser approach (steps 1-6 working, blocked at embed provider detection)
- `auto_download_vivaldi.py` — Vivaldi + nodriver approach (fully working end-to-end)
- `screenshots/` — debug screenshots from each run (gitignored)
- Extension: `C:\dev\thunderhead\browser-extension\hls-capture`
- CloakBrowser binary: `C:\Users\thunderhead\.cloakbrowser\chromium-145.0.7632.159.7\chrome.exe`
- Vivaldi: `C:\Users\thunderhead\AppData\Local\Vivaldi\Application\vivaldi.exe`
- Automation profile: `C:\Temp_Media\_vivaldi_automation`
- Download output: `C:\Temp_Media\Movies\` (movies), `C:\Temp_Media\TV Shows\` (shows)
