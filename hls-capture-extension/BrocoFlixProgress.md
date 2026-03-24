# BrocoFlix Browser-Side Download — Progress Log

## Status: WORKING — movies download at ~99.4% completion (2026-03-24)

Movies and TV episodes both download successfully. TV episodes achieve 100%. Movies achieve ~99.3-99.4% due to persistent per-IP CDN segment blocking that cannot be recovered by any retry strategy tested so far.

## What Works
- Full relay chain: MAIN-world iframe → postMessage → content script → background → server
- Server receives and writes chunks to disk (confirmed with server-side chunk logging)
- Session lifecycle: `/brocoflix-start` → `/brocoflix-chunk` → `/brocoflix-done` (mux) or `/brocoflix-abort`
- Popup shows "uploading" status with chunk progress
- Client-side dedup (`brocoflixActiveUrls` Set) prevents duplicate `startBrocoflixDownload` calls
- Server-side dedup (`seen_urls`) catches any that slip through
- Confirmation popup flow: BrocoFlix queues m3u8 into `pendingCaptures` like 1movies, shows in-page dialog for 720p+, user confirms before download starts. Quality probed from iframe MAIN-world (CDN blocks server-side yt-dlp probing).
- Iframe reload recovery: skip-and-continue downloading + automatic iframe reload to get fresh CDN domain + fresh Chrome socket pool. Proactive reload every 800 segments. Retry passes with 10s delay.
- Auto-click play button after iframe reload (`#btn-play` selector + `autoPlay=true` URL param)
- Simplified file naming (title only, no year) with `fix_file_names.py` running post-mux
- Files saved to root staging dirs (`C:\Temp_Media\Movies` and `C:\Temp_Media\TV Shows`)
- Stall detection and give-up: after 3 stalled reloads at ≥99.5%, or 5 stalled reloads below target, or 15 total reloads → force-mux whatever we have

## Architecture Overview

### How the download works
1. `webRequest.onBeforeRequest` intercepts m3u8 URL from BrocoFlix embed iframe
2. m3u8 goes into pending queue → quality probed from iframe → preview dialog shown
3. User confirms → `startBrocoflixDownload()` called with tabId and frameId
4. `chrome.scripting.executeScript({ world: "MAIN" })` injects `brocoflixDownloaderFunc` into the embed iframe (origin: `streameeeeee.site` or `vidsrc.cc`)
5. Injected function runs in iframe's MAIN world — CDN accepts requests from this origin
6. Downloads each TS segment sequentially via `fetch()`, converts to base64, sends via `window.postMessage`
7. Content script (ISOLATED world) receives postMessage → `chrome.runtime.sendMessage` → background service worker
8. Background decodes base64, POSTs raw binary to `/brocoflix-chunk` on localhost:9876
9. On completion: `/brocoflix-done` → server runs `ffmpeg -i temp.ts -c copy -movflags +faststart output.mp4`
10. Server runs `fix_file_names.py` post-mux to normalize filename via OMDb

### Why this relay chain is necessary
- CDN domains (silvercloud9.pro, mistwolf88.xyz, bluehorizon4.site, stormfox27.live, etc.) **403 ALL non-browser clients** (yt-dlp, curl, wget, service worker fetch)
- MAIN-world can't POST to localhost (mixed content: HTTPS page → HTTP server, plus CSP)
- MV3 content scripts share the page's network context, can't fetch arbitrary URLs
- Background service worker can fetch localhost freely

### Iframe reload recovery
When segments fail mid-download, the downloader:
1. Skips failed segments and continues downloading remaining segments
2. Proactively reloads iframe every 800 segments (before GOAWAY threshold)
3. After pass completes, reports failed segments to background via `hlsBrocoNeedReload` postMessage
4. Background reloads only the embed iframe (not the full tab) — clears src, restores after 500ms
5. New m3u8 intercepted on fresh CDN domain → re-injects downloader with `completedIndices` skip list
6. Retry pass: only fetches missing segments with 10s delay between each
7. Each reload cancels previous reload's safety timer (prevents stale timer abort bug)
8. Stall detection: if no progress after multiple reloads, force-mux whatever we have

## The CDN Rate Limiting Problem

### What happens
The CDN blocks ~0.6% of segment fetches during movie downloads. The error is always instant "Failed to fetch" (network-level connection reset, not HTTP status, not timeout). Once a specific segment index fails for a given movie, that segment index is permanently blocked for the current IP address — no retry strategy within the same browser session or across CDN domain rotations has ever recovered a persistently blocked segment.

### Failure patterns observed
- **TV episodes succeed at 100%**: Survivor S50E04 (1088 segments) — only 2 transient failures, both recovered with 3s backoff
- **Movies hit ~99.3-99.4%**: Persistent failures at specific segment indices that never recover
- **Failure rate**: ~0.6% of segments per movie (5-9 persistent failures per 881-1528 segments)
- **First failure timing**: Varies by content — segment 114 for Rambo III, segment 261 for The 'Burbs, always the same index for the same movie
- **Failure spacing**: At 500ms pace, new failures appear every ~80-150 segments
- **Failures are instant**: "Failed to fetch" is a connection-level error. Increasing fetch timeout from 30s to 120s makes no difference — error is immediate
- **Deterministic per-content**: Same segment indices fail on every attempt for the same movie, across all CDN domains

### Two types of failures
1. **Transient failures** (~50% of initial failures): Recover on iframe reload with fresh CDN domain. These include "signal is aborted without reason" (fetch timeout at 30s) and some "Failed to fetch" errors.
2. **Persistent failures** (~50% of initial failures): Never recover across any number of reloads or CDN domain rotations. Appear to be per-IP, per-content blocks at the CDN backend.

## Test Results

### Successful Downloads

#### TV Episode: Survivor S50E04 (~1088 segments) — 2026-03-19
| Segments | Throttle | Result |
|----------|---------|--------|
| 1088/1088 ✅ | 200ms→1500ms after first failure | **100% SUCCESS** — only 2 retries (seg 348, 471), both succeeded with 3s backoff. Killing video player was the key fix. |

#### Movie: Send Help (~1362 segments) — 2026-03-23
| Segments | Reloads | Result |
|----------|---------|--------|
| 1362/1362 ✅ | 1 (proactive at 800) | **100% SUCCESS** — first movie to complete. All failures were transient and recovered on reload. |

#### Movie: Rambo III (~1528 segments) — 2026-03-24
| Segments | Reloads | Result |
|----------|---------|--------|
| 1519/1528 (99.4%) | 8 (1 proactive + 7 retry) | **COMPLETED** — 9 persistent failures: [114, 119, 220, 770, 968, 1039, 1073, 1117, 1497]. Muxed successfully. |

Reload progression for Rambo III:
- Pass 1: 800/1528 done, 5 skipped → proactive reload at 800
- Reload #1: recovered 367 (timeout) from first pass. New failures: 968, 1039, 1073, 1117, 1170, 1497. Total 1518/1528.
- Reload #2: retry pass recovered 1170 (timeout) and 1467. Total 1519/1528.
- Reloads #3-7: same 9 segments failed every time across domains stormfox27.live, mistwolf88.xyz, bluehorizon4.site, silvercloud9.pro. Zero progress.
- Reload #8: stalled=5/3, gave up and force-muxed.

#### Movie: Conan the Barbarian (~1479 segments) — 2026-03-24
Download started, proactive reload at 800 worked, but aborted due to stale reload timer bug (timer from reload #2 fired during reload #4). Timer bug was fixed mid-session. Did not re-test to completion.

### Failed Downloads (Pre-Iframe-Reload Era, 2026-03-15 to 2026-03-23)

All movie downloads before the iframe reload strategy was implemented failed completely. Segments that failed never recovered within the same browser session regardless of retry strategy.

#### The 'Burbs (~881 segments) — 2026-03-22/23, 7 attempts
| Attempt | Strategy | Result |
|---------|----------|--------|
| 1 | Unlimited retry + exponential backoff (3-120s) | Stuck forever on segment 261 |
| 2 | Skip after 3 tries + separate retry passes (30s cooldown) | First pass: skipped 5 segs (261,343,447,545,607). All 5 retry passes failed — none recovered |
| 3 | Interleaved retry (re-queue 20 segs later) | Segment 261 failed all 8 attempts despite 20 successful segments between each retry |
| 4 | Interleaved retry + manifest refresh on failure | Same — fresh manifest URLs didn't help |
| 5 | 90s cooldown + re-queue 60 segs later | Same — 90s pause didn't help |
| 6 | Same + `cache: "no-store"` on fetch | Same |
| 7 | Same + cache-busting query param (`?_r=timestamp`) | Same |

#### The 'Burbs — 2026-03-24 (with iframe reload)
| Segments | Reloads | Result |
|----------|---------|--------|
| 875/881 (99.3%) | Multiple | 6 persistent failures: [261, 343, 447, 545, 607, + 1 other]. Same indices as pre-reload attempts. |

#### Rambo III (~1528 segments) — 2026-03-22/23, pre-reload
| Attempt | Strategy | Result |
|---------|----------|--------|
| 1 | 90s cooldown + re-queue 60 segs later | Failed at segment 114 (all 5 attempts). Also: 119, 127, 220, 282 |
| 2 | Same + 120s fetch timeout | Same — errors are instant, not timeouts |
| 3 | Same + cache-busting query params | Same |

#### Rocky (~1170-1434 segments) — 2026-03-15 to 2026-03-17
| Date | Segments | Strategy | Result |
|------|----------|----------|--------|
| 2026-03-15 | 101/1170 | No retries, no throttle | Failed to fetch |
| 2026-03-16 | 342/1217 | 3 retries, 2s/6s backoff, 100ms throttle | Failed after 3 attempts |
| 2026-03-17 | 127/1170 | 5 retries, 2-16s backoff, 200ms throttle | Failed after 5 attempts |
| 2026-03-17 | 342/1217 | 5 retries + manifest refresh, 200ms→1s throttle | Failed after 5 attempts |
| 2026-03-17 | ~100/1434 | Unlimited retries, 2-60s backoff | Stuck in retry loop (never recovered) |

## What Has Been Tried and Ruled Out

### Retry strategies that don't work (within same browser session)
1. **Exponential backoff** (3s to 120s): Failed segments never recover regardless of wait time
2. **Interleaved retry** (re-queue N segments later): Failed segments still fail even after 20-60 successful segments between attempts
3. **Manifest refresh**: Getting fresh segment URLs (new manifest → new CDN paths for same segment index) doesn't help
4. **Longer fetch timeout** (30s → 120s): Errors are instant connection resets, not timeouts
5. **Cache bypass** (`cache: "no-store"` on fetch): Doesn't help
6. **Cache-busting query params** (`?_r=timestamp`): Doesn't help
7. **90s cooldown pause**: Doesn't help — CDN rate-limit window should be clear but segments stay blocked
8. **Multiple retry passes with delays**: Same segments fail on every pass

### Retry strategies that DO work
1. **Iframe reload** (fresh CDN domain + fresh Chrome socket pool): Recovers ~50% of failed segments per reload. Transient failures recover; persistent failures don't.
2. **Killing the video player** before downloading: Frees CDN bandwidth/connection slots. Was the key fix for TV episode success.

### Fetch API variations tried
1. `fetch()` with default options → "Failed to fetch"
2. `fetch()` with `cache: "no-store"` → same
3. `fetch()` with `cache: "no-store"` + AbortController timeout → same (plus "signal is aborted" for slow segments at 15s timeout — too aggressive)
4. `fetch()` with cache-busting query params → same

### Full sequential re-fetch on retry pass (2026-03-24)
Tried fetching ALL segments sequentially on retry passes (including already-completed ones) so the CDN would see normal playback access patterns instead of cherry-picked retry requests. **Did not help** — the same persistent segments still failed even when buried in a sequential stream of successful fetches. This rules out CDN access-pattern detection as the blocking mechanism.

### Pacing variations
- 0ms (no throttle): ~100 segments before first failure
- 100ms: ~340 segments before first failure
- 200ms: ~260-350 segments before first failure
- 500ms (current): ~260-800 segments before first failure, fewer total failures
- 1500ms: ~80-100 segments between new failures (paradoxically, failures still occur at roughly the same total count)

### Fetch timeout tuning
- 15s: Too aggressive — causes false-positive "signal is aborted" failures on slow-but-valid segments (~6 extra failures per 1500 segments). These recover on reload but waste reload cycles.
- 30s (current): Good balance — only catches genuinely dead connections
- 120s: Too long — hanging fetches freeze the download for 2 minutes per stuck segment

## Root Cause Analysis

### Confirmed: HTTP/2 GOAWAY + Chrome socket pool poisoning
The "permanently poisoned segments" within a single browser session are caused by Chrome's handling of HTTP/2 GOAWAY frames. When the CDN's NGINX sends a GOAWAY (triggered by `keepalive_requests` limit, typically ~1000), Chrome's socket pool marks those in-flight streams as permanently failed. Chrome bug #681477 documents that streams aborted by GOAWAY are NOT retried — they permanently fail as "Failed to fetch" in the same socket pool.

**Evidence:**
- Iframe reload (which creates fresh socket pool via new origin) recovers ~50% of failures
- Same segment indices fail deterministically for the same content
- Error is instant connection-level, not HTTP status

### Unconfirmed: Per-IP per-content CDN rate limiting
The ~50% of failures that persist even across iframe reloads (fresh CDN domains, fresh socket pools) appear to be rate-limited at a layer above the individual CDN edge server. This blocking is:
- **Per-IP**: Same segments blocked from same IP regardless of CDN domain
- **Per-content**: Different movies have different blocked segment indices (not a global rate limit)
- **Persistent**: Blocked segments never recover across any tested delay (up to minutes between retries)
- **Not pattern-based**: Full sequential re-fetch (blending retries into normal playback stream) didn't help

## Current Implementation (as of 2026-03-24)

### Download flow (`brocoflixDownloaderFunc` in background.js)
- Kills video player (`<video>` elements + JWPlayer/HLS.js) before starting download
- Sequential download at 500ms/segment base pace
- Skip-and-continue: failed segments are skipped, download continues to end of manifest
- Proactive iframe reload every 800 segments (before GOAWAY threshold)
- After pass completes with failures → request iframe reload
- Retry pass (≤20 remaining segments): 10s between each fetch, `MAX_CONSECUTIVE_FAILS = remaining + 1` (complete the full loop, don't break early)
- `fetchWithTimeout` default 30s, `cache: "no-store"`
- Chunks sent to server in order via `flushToServer()` buffer

### Iframe reload mechanism
- Background clears iframe src, waits 500ms, restores with `autoPlay=true` URL param
- Auto-clicks `#btn-play` button via `chrome.scripting.executeScript({ allFrames: true })`
- Each reload cancels previous reload's safety timer (fixes stale timer abort bug)
- 120s safety timeout per reload (aborts if no new m3u8 intercepted)
- `seenM3u8` cleared before reload to allow re-interception

### Give-up logic
- `TARGET_PCT = 99.5%`
- `MAX_NO_PROGRESS_RELOADS = 3` — stall counter increments when no new segments recovered
- `MAX_TOTAL_RELOADS = 15` — hard limit
- Give up when: (meets 99.5% AND stalled ≥3) OR (stalled ≥5 even if below target) OR (reloads ≥15)
- On give-up: POST `/brocoflix-done` → server muxes whatever chunks were received

### Confirmation popup
- BrocoFlix m3u8 goes through same pending/preview/confirm flow as 1movies
- Quality probed from iframe MAIN-world via `probeBrocoflixQuality()` (CDN blocks server-side yt-dlp)
- Server `/preview` endpoint accepts optional `quality` field to skip yt-dlp probe
- Episode context populated from DOM `<h1>` in `fetchPreview()` (movies have no card click)
- Stale context detection via `_pageUrl` field

### Server changes (`hls_download_server.py`)
- `brocoflix_start`: Simplified output paths — Movies: `C:\Temp_Media\Movies\{title}.mp4`, TV: `C:\Temp_Media\TV Shows\{title} SxxExx.mp4` (no year, no subfolder)
- `brocoflix_chunk`: Uses `session["chunks_received"] += 1` (not `chunk_index + 1`) for accurate counting with non-contiguous chunks
- `brocoflix_done`: Runs `fix_file_names.py` post-mux to normalize filename via OMDb

### Log Relay
MAIN-world logs are invisible (BrocoFlix blocks F12 devtools). All `[BF-dl]` logs relay through:
`relayLog()` → `postMessage(hlsBrocoLog)` → content script → `chrome.runtime.sendMessage(brocoflixLog)` → background `console.log`

Retry/progress logs appear in the **service worker console**.

### Server Chunk Logging
`brocoflix_chunk()` prints progress every 50 chunks + first + last chunk:
```
[MOVIE] BrocoFlix chunk 50/1434 (76.5MB received)
```

## Bugs Found and Fixed During Testing

1. **chrome.tabs.reload() navigated away from player** (2026-03-23): Full tab reload caused BrocoFlix to return to movie info page. Fix: reload only the embed iframe by clearing/restoring its src.

2. **Play button not clicked after iframe reload** (2026-03-23): Embed player showed play button overlay requiring manual click. Fix: auto-click `#btn-play` via `chrome.scripting.executeScript({ allFrames: true })` + `autoPlay=true` URL param.

3. **chrome.webNavigation.getAllFrames undefined** (2026-03-23): Used without required permission. Fix: replaced with `chrome.scripting.executeScript` which needs no extra permission.

4. **Infinite reload loop on persistent failures** (2026-03-23): Stop-on-first-failure caused infinite reload loops when specific segments were permanently blocked. Fix: skip-and-continue strategy — download all segments, skip failures, then reload once to retry.

5. **"Cannot access 'isRetryPass' before initialization"** (2026-03-23): `relayLog` referenced `isRetryPass` before `const` declaration. Fix: moved declarations above the log line.

6. **Give-up condition never fires** (2026-03-23): `meetsTarget && stalled >= 3` never triggered because 876/881 = 99.4% < 99.5%. Added fallback: give up after 5 stalled reloads even if below target.

7. **Retry pass triggers "connection dead" prematurely** (2026-03-23): `MAX_CONSECUTIVE_FAILS = remaining` on retry pass with 5 remaining meant all 5 fails triggered early exit before completing the loop. Fix: `MAX_CONSECUTIVE_FAILS = remaining + 1`.

8. **Stale reload timer abort** (2026-03-24): Timer from reload #N fired during reload #N+1's retry pass (which takes ~90s for 9 segments × 10s). Fix: store timer ID on session, cancel previous timer before setting new one.

9. **Fetch timeout too aggressive at 15s** (2026-03-24): Caused ~6 extra "signal is aborted" false-positive failures per 1500 segments. Fix: increased to 30s.

## Duplicate m3u8 Interception (Minor Issue)
Each page load fires 2-3 webRequest events for the same m3u8 URL. `seenM3u8` Set catches most duplicates, but 2 can race into `chrome.tabs.get` async callback before either adds to `seenM3u8`. Both get queued as pending. Server-side dedup handles it cleanly — the noise is cosmetic only (second pending shows "unknown" quality probe).

## Key Technical Details

### CDN infrastructure
- **Embed origins**: `streameeeeee.site`, `vidsrc.cc` — the iframe origin the CDN trusts
- **CDN URL patterns**: `https://<random-domain>/file1/<base64-token>` and `https://<domain>/pl/<base64-token>`
- **CDN domains rotate every page load**: silvercloud9.pro, mistwolf88.xyz, bluehorizon4.site, stormfox27.live, dustfalcon55.xyz, icynebula71.pro, solarwolf23.live, etc.
- **All CDN domains share the same backend**: Per-IP rate limits persist across domain rotations
- **CDN blocks non-browser clients**: yt-dlp, curl, wget, service worker fetch all get 403

### Browser constraints
- **BrocoFlix blocks F12 devtools**: All debugging through service worker console or server logs
- **CORS for localhost from iframe**: Blocked — relay chain required
- **Chrome HTTP/2 GOAWAY bug** (#681477): Failed streams permanently poisoned in socket pool
- **MV3 content scripts**: Share page's network context, can't fetch arbitrary URLs

### Content detection
- Movie pages: No `.episode-card`, title from DOM `<h1>` in `#details-container`
- Page URL contains type: `?type=movie` or `?type=tv` for routing
- Movie output: `C:\Temp_Media\Movies\{title}.mp4`
- TV output: `C:\Temp_Media\TV Shows\{title} SxxExx.mp4`

### FetchV extension reference
An existing HLS downloader extension (FetchV) can download from BrocoFlix. It sometimes errors but has a "convert to single thread" button that usually allows it to complete the download. This suggests 100% completion is achievable from JavaScript in the browser.
