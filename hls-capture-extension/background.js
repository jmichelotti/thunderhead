const SERVER_URL = "http://localhost:9876/capture";
const PREVIEW_URL = "http://localhost:9876/preview";
const SUBTITLE_URL = "http://localhost:9876/subtitle";
const SEASON_INFO_URL = "http://localhost:9876/season-info";

// ========= SITE CONFIGS =========

const SITE_CONFIGS = {
  "1movies": {
    navStrategy: "hash-reload",         // navigate via location.hash + reload
    makeEpisodeHash: (s, e) => `#ep=${s},${e}`,
    playButtonSelector: "#player button.player-btn",
    subtitleWaitMs: 4000,
  },
  "brocoflix.xyz": {
    navStrategy: "click-card",          // navigate by clicking .episode-card elements
    playButtonSelector: ".episode-play-button", // inside each episode card
    serverButtonSelector: ".server-button",     // Nth button = server N (1-indexed)
    subtitleWaitMs: 5000,
    showTitleSelector: "#details-container h1",
    episodeCardSelector: ".episode-card",
    seasonSelectSelector: "#season-select",
  },
};

function getSiteConfig(url) {
  if (!url) return null;
  try {
    const host = new URL(url).hostname;
    for (const [site, cfg] of Object.entries(SITE_CONFIGS)) {
      if (host.includes(site)) return { site, ...cfg };
    }
  } catch {}
  return null;
}

// ========= STATE =========

// Pending captures awaiting user confirmation: [{m3u8_url, page_url, tabId, timestamp, preview}]
let pendingCaptures = [];

// Confirmed/active captures: [{m3u8_url, page_url, tabId, timestamp, status, message}]
let captures = [];

// Track subtitle URLs per tab to avoid duplicates
const subtitlesSent = new Set();

// Track m3u8 URLs already pending or confirmed to avoid duplicates
const seenM3u8 = new Set();

// Per-tab episode context set by content script for DOM-based sites (e.g. brocoflix)
// Map<tabId, {show_name, season, episode}>
const episodeContextByTab = new Map();

// Auto-capture state
let autoCapture = {
  active: false,
  finished: false,
  tabId: null,
  season: null,
  startEp: null,
  endEp: null,
  currentEp: null,
  doneCount: 0,
  totalCount: 0,
  siteConfig: null,
  serverNum: 1,
  graceUntil: 0,  // timestamp: auto-confirm still fires during grace period after last ep
  epoch: 0,              // increments each episode advance; used to detect stale done-signals
  episodeDoneSent: false, // true after first done-signal for current epoch (prevents duplicates)
  // Multi-season fields
  multiSeason: false,
  startSeason: null,
  endSeason: null,
  currentSeason: null,
  // DOM-based episode discovery (hash-reload sites)
  episodeHashes: [],     // discovered episode list for current season [{hash, epStart, epEnd}]
};

// ========= NETWORK INTERCEPTION =========

// Listen for m3u8 and subtitle requests
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.type === "main_frame") return;

    const url = details.url;
    const tabId = details.tabId;

    // Check for subtitle files (.vtt, .srt)
    if (/\.(vtt|srt)(\?|$)/i.test(url)) {
      if (subtitlesSent.has(url)) return;
      subtitlesSent.add(url);

      chrome.tabs.get(tabId, (tab) => {
        if (chrome.runtime.lastError) return;
        sendSubtitle(url, tab?.url || "", tabId);
      });
      return;
    }

    // Check for m3u8 (filter out tracking pixels that happen to contain .m3u8 in query params)
    if (!url.includes(".m3u8")) return;
    if (/\.(gif|png|jpg|jpeg)(\?|$)/i.test(url.split("?")[0])) return;

    // Skip if already pending or confirmed
    if (seenM3u8.has(url)) {
      if (autoCapture.active) console.log(`[AC] m3u8 blocked by seenM3u8 (ep ${autoCapture.currentEp}, epoch ${autoCapture.epoch}): ${url.slice(0, 80)}`);
      return;
    }
    seenM3u8.add(url);

    // Get the tab URL for context
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) return;

      const pageUrl = tab?.url || "";
      const m3u8Domain = (() => { try { return new URL(url).hostname; } catch { return "?"; } })();
      const siteCtx = getSiteConfig(pageUrl);
      const epCtx = episodeContextByTab.get(tabId);
      console.log(`[HLS] m3u8 intercepted: domain=${m3u8Domain} site=${siteCtx?.site || "unknown"} tab=${tabId} url=${url.slice(0, 200)}`);
      console.log(`[HLS]   episodeContext: ${epCtx ? JSON.stringify(epCtx) : "NONE (not set for this tab)"}`);
      console.log(`[HLS]   pageUrl: ${pageUrl}`);

      // BrocoFlix: use browser-side download (CDN blocks non-browser clients)
      // During auto-capture, start immediately (no confirmation needed).
      // Otherwise, queue into pending like 1movies so user gets a confirmation dialog.
      if (siteCtx?.site === "brocoflix.xyz" && details.frameId > 0) {
        // Check if this m3u8 is from an iframe reload for connection recovery.
        // If so, resume the existing download with the new m3u8 URL + fresh frameId.
        for (const [sid, rs] of brocoflixReloadState.entries()) {
          if (rs.tabId === tabId) {
            console.log(`[BF] New m3u8 intercepted after reload for session ${sid}. Resuming with ${rs.completedIndices.length} completed segments.`);
            brocoflixReloadState.delete(sid);
            // Update the session's m3u8Url to the new one (new CDN domain)
            const session = brocoflixSessions.get(sid);
            if (session) {
              session.m3u8Url = url;
              session.frameId = details.frameId;
            }
            startBrocoflixDownload(url, pageUrl, tabId, details.frameId, {
              sessionId: sid,
              completedIndices: rs.completedIndices,
            });
            return;
          }
        }

        if (tabId === autoCapture.tabId &&
            (autoCapture.active || Date.now() < autoCapture.graceUntil)) {
          startBrocoflixDownload(url, pageUrl, tabId, details.frameId);
          return;
        }
        // Fall through to pending queue — store frameId for use at confirmation time
        brocoflixPendingFrameId = details.frameId;

        // Clear stale episode context if the page URL changed (e.g. navigated
        // from one movie to another on the same tab)
        const existingCtx = episodeContextByTab.get(tabId);
        if (existingCtx && existingCtx._pageUrl && existingCtx._pageUrl !== pageUrl) {
          episodeContextByTab.delete(tabId);
        }

        // Episode context is populated in fetchPreview for BrocoFlix
        // (needs to be awaited, can't do it reliably in this sync callback)
      }

      // Auto-capture mode: skip pending queue, auto-confirm immediately.
      // Also fires during the grace period after the last episode, in case the
      // video was slow to start and the content script timed out before the m3u8 fired.
      if (tabId === autoCapture.tabId &&
          (autoCapture.active || Date.now() < autoCapture.graceUntil)) {
        // Verify page URL hash matches expected episode.
        // 1movies.bz redirects non-existent episodes to #ep=1,1 — without
        // this check the wrong content gets auto-confirmed and the skip
        // counter never triggers, causing an infinite loop on S01E01.
        if (autoCapture.episodeHashes.length > 0) {
          // Discovery mode: currentEp is a 1-based index into episodeHashes,
          // so validate by comparing the page hash against the discovered hash.
          const epIdx = autoCapture.currentEp - 1;
          const expectedHash = epIdx >= 0 && epIdx < autoCapture.episodeHashes.length
            ? autoCapture.episodeHashes[epIdx].hash
            : null;
          if (expectedHash) {
            const pageHashMatch = pageUrl.match(/#ep=\d+,\d+(?:-\d+)?/);
            if (pageHashMatch && pageHashMatch[0] !== expectedHash) {
              console.log(`[AC] m3u8 REJECTED: page hash ${pageHashMatch[0]} doesn't match expected ${expectedHash}`);
              return;
            }
          }
        } else {
          // Fallback (no discovery): currentEp IS the actual episode number,
          // so compare numerically.  Combined episodes use #ep=season,start-end.
          const hashMatch = pageUrl.match(/#ep=(\d+),(\d+)(?:-(\d+))?/);
          if (hashMatch) {
            const actualSeason = parseInt(hashMatch[1], 10);
            const actualEpStart = parseInt(hashMatch[2], 10);
            const actualEpEnd = hashMatch[3] ? parseInt(hashMatch[3], 10) : actualEpStart;
            const expectedSeason = autoCapture.multiSeason ? autoCapture.currentSeason : autoCapture.season;
            const expectedEp = autoCapture.currentEp;
            if (actualSeason !== expectedSeason || expectedEp < actualEpStart || expectedEp > actualEpEnd) {
              console.log(`[AC] m3u8 REJECTED: page hash S${actualSeason}E${actualEpStart}${hashMatch[3] ? "-" + actualEpEnd : ""} doesn't cover expected S${expectedSeason}E${expectedEp}`);
              return;
            }
          }
        }
        console.log(`[AC] m3u8 intercepted for ep ${autoCapture.currentEp} (epoch ${autoCapture.epoch}): ${url.slice(0, 80)}`);
        autoConfirmCapture(url, pageUrl, tabId);
        return;
      }

      const pending = {
        m3u8_url: url,
        page_url: pageUrl,
        tabId: tabId,
        frameId: brocoflixPendingFrameId || null,  // BrocoFlix needs this at confirm time
        timestamp: Date.now(),
        preview: null,
        previewStatus: "loading",
      };
      brocoflixPendingFrameId = null;  // consumed
      pendingCaptures.push(pending);

      // Keep only last 20 pending
      if (pendingCaptures.length > 20) {
        pendingCaptures = pendingCaptures.slice(-20);
      }

      const index = pendingCaptures.length - 1;

      updateBadge();

      // Don't show dialog yet — wait for preview to determine quality.
      // Only 720p+ will get an on-page dialog; lower quality stays in popup only.
      fetchPreview(pending, index);
    });
  },
  { urls: ["<all_urls>"] },
  []
);

// ========= PREVIEW / CONFIRM / SEND =========

// Probe m3u8 resolution from within the embed iframe (CDN blocks server-side probing).
// Returns a string like "1920x1080" or null.
async function probeBrocoflixQuality(tabId, frameId, m3u8Url) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId, frameIds: [frameId] },
      world: "MAIN",
      func: (url) => {
        return fetch(url).then(r => r.text()).then(manifest => {
          let best = null, bestH = 0;
          for (const line of manifest.split("\n")) {
            const m = line.match(/RESOLUTION=(\d+)x(\d+)/);
            if (m) {
              const h = parseInt(m[2], 10);
              if (h > bestH) { bestH = h; best = m[1] + "x" + m[2]; }
            }
          }
          return best;
        }).catch(() => null);
      },
      args: [m3u8Url],
    });
    return results?.[0]?.result || null;
  } catch (err) {
    console.log(`[BF] Quality probe failed: ${err.message}`);
    return null;
  }
}

async function fetchPreview(pending, index) {
  // For BrocoFlix, ensure episode context exists (movies have no card click to set it)
  // and probe quality from the browser (CDN 403s yt-dlp)
  let browserQuality = null;
  if (pending.frameId) {
    if (!episodeContextByTab.has(pending.tabId)) {
      try {
        const results = await chrome.scripting.executeScript({
          target: { tabId: pending.tabId },
          func: () => {
            const h1 = document.querySelector("#details-container h1");
            return h1?.textContent?.trim() || document.title.replace(/\s*[\|–\-].*$/, "").trim();
          },
        });
        const title = results?.[0]?.result || "";
        if (title) {
          const isMovie = pending.page_url.includes("type=movie");
          episodeContextByTab.set(pending.tabId, {
            show_name: title,
            season: isMovie ? null : 1,
            episode: isMovie ? null : 1,
            _pageUrl: pending.page_url,  // track for stale context detection
          });
          console.log(`[BF] Set episode context from DOM: "${title}" isMovie=${isMovie}`);
        }
      } catch (err) {
        console.log(`[BF] Failed to get page title: ${err.message}`);
      }
    }
    browserQuality = await probeBrocoflixQuality(pending.tabId, pending.frameId, pending.m3u8_url);
    console.log(`[BF] Browser quality probe: ${browserQuality || "unknown"}`);
  }

  const ctx = episodeContextByTab.get(pending.tabId) || {};

  try {
    const resp = await fetch(PREVIEW_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        m3u8_url: pending.m3u8_url,
        page_url: pending.page_url,
        show_name: ctx.show_name || "",
        season: ctx.season ?? null,
        episode: ctx.episode ?? null,
        quality: browserQuality,  // server skips yt-dlp probe when provided
      }),
    });

    const data = await resp.json();
    if (data.status === "ok") {
      pending.preview = data;
      pending.previewStatus = "ready";
    } else {
      pending.previewStatus = "error";
      pending.previewError = data.message || "Preview failed";
    }
  } catch (err) {
    pending.previewStatus = "error";
    pending.previewError = "Server not running? " + err.message;
  }

  updateBadge();

  // Show on-page dialog only for 720p+ content
  const currentIndex = pendingCaptures.indexOf(pending);
  if (currentIndex >= 0 && isHighQuality(pending.preview)) {
    notifyTab(pending.tabId, {
      type: "showCaptureDialog",
      index: currentIndex,
      previewStatus: pending.previewStatus,
      previewError: pending.previewError || null,
      preview: pending.preview,
    });
  }
}

function notifyTab(tabId, message) {
  if (!tabId || tabId < 0) return;
  chrome.tabs.sendMessage(tabId, message).catch(() => {
    // Content script not loaded yet, ignore
  });
}

function isHighQuality(preview) {
  if (!preview || !preview.quality) return false;
  // quality is like "1920x1080 mp4" — extract height
  const match = preview.quality.match(/(\d+)x(\d+)/);
  if (!match) return false;
  const height = parseInt(match[2], 10);
  return height >= 720;
}

async function confirmDownload(index) {
  if (index < 0 || index >= pendingCaptures.length) return;

  const pending = pendingCaptures.splice(index, 1)[0];

  // BrocoFlix: route to browser-side download instead of yt-dlp
  if (pending.frameId) {
    updateBadge();
    startBrocoflixDownload(pending.m3u8_url, pending.page_url, pending.tabId, pending.frameId);
    return;
  }

  const capture = {
    m3u8_url: pending.m3u8_url,
    page_url: pending.page_url,
    tabId: pending.tabId,
    timestamp: Date.now(),
    status: "sending",
  };
  captures.push(capture);

  if (captures.length > 50) {
    captures = captures.slice(-50);
  }

  updateBadge();
  await sendToServer(capture);
}

function dismissCapture(index) {
  if (index < 0 || index >= pendingCaptures.length) return;
  const removed = pendingCaptures.splice(index, 1)[0];
  // Allow this URL to be recaptured if it appears again
  seenM3u8.delete(removed.m3u8_url);
  updateBadge();
}

async function sendToServer(capture) {
  const ctx = episodeContextByTab.get(capture.tabId) || {};
  try {
    const resp = await fetch(SERVER_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        m3u8_url: capture.m3u8_url,
        page_url: capture.page_url,
        show_name: ctx.show_name || "",
        season: ctx.season ?? null,
        episode: ctx.episode ?? null,
      }),
    });

    const data = await resp.json();
    capture.status = data.status || "sent";
    capture.message = data.message || "";
    console.log(`[HLS] sendToServer response: status=${data.status} message=${data.message || ""} url=${capture.m3u8_url.slice(0, 80)}`);
  } catch (err) {
    capture.status = "error";
    capture.message = "Server not running? " + err.message;
    console.log(`[HLS] sendToServer ERROR: ${err.message}`);
  }

  updateBadge();
}

async function sendSubtitle(subtitleUrl, pageUrl, tabId) {
  const ctx = tabId != null ? (episodeContextByTab.get(tabId) || {}) : {};
  try {
    await fetch(SUBTITLE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subtitle_url: subtitleUrl,
        page_url: pageUrl,
        show_name: ctx.show_name || "",
        season: ctx.season ?? null,
        episode: ctx.episode ?? null,
      }),
    });
  } catch {
    // Server not running, ignore
  }
}

// ========= BADGE =========

function updateBadge() {
  // Auto-capture mode: show progress
  if (autoCapture.active) {
    const text = autoCapture.totalCount > 0
      ? `${autoCapture.doneCount}/${autoCapture.totalCount}`
      : `${autoCapture.doneCount}`;
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color: "#4338ca" });
    return;
  }

  const pendingCount = pendingCaptures.length;
  const activeCount = captures.filter(
    (c) => c.status === "sending" || c.status === "downloading"
  ).length;

  if (pendingCount > 0) {
    chrome.action.setBadgeText({ text: String(pendingCount) });
    chrome.action.setBadgeBackgroundColor({ color: "#f59e0b" });
  } else if (activeCount > 0) {
    chrome.action.setBadgeText({ text: String(activeCount) });
    chrome.action.setBadgeBackgroundColor({ color: "#6366f1" });
  } else if (captures.length > 0) {
    chrome.action.setBadgeText({ text: String(captures.length) });
    chrome.action.setBadgeBackgroundColor({ color: "#22c55e" });
  } else {
    chrome.action.setBadgeText({ text: "" });
  }
}

// ========= AUTO-CAPTURE =========

async function autoConfirmCapture(m3u8Url, pageUrl, tabId) {
  // Snapshot the epoch so we can detect if we've advanced past this episode
  // by the time the download + delay finishes
  const epoch = autoCapture.epoch;
  const ep = autoCapture.currentEp;
  console.log(`[AC] autoConfirmCapture START ep=${ep} epoch=${epoch} url=${m3u8Url.slice(0, 80)}`);

  const capture = {
    m3u8_url: m3u8Url,
    page_url: pageUrl,
    tabId: tabId,
    timestamp: Date.now(),
    status: "sending",
  };
  captures.push(capture);

  if (captures.length > 50) {
    captures = captures.slice(-50);
  }

  updateBadge();
  await sendToServer(capture);
  console.log(`[AC] autoConfirmCapture server responded ep=${ep} status=${capture.status}`);

  // Wait a moment for any remaining subtitle requests to arrive at the server
  // (subtitles load on page init but some may still be in-flight)
  await new Promise((r) => setTimeout(r, 2000));

  // Guard: only send done-signal if we're still on the same episode AND
  // haven't already sent one for this episode.  Multiple m3u8 URLs per episode
  // (ad pre-rolls, quality variants, CDN retries) each trigger this function,
  // but only the first should fire the done-signal.  Without this guard, a
  // stale done-signal from episode N can resolve episode N+1's
  // waitForEpisodeDone, causing it to be skipped.
  if (epoch !== autoCapture.epoch || autoCapture.episodeDoneSent) {
    console.log(`[AC] autoConfirmCapture SUPPRESSED ep=${ep} snapshotEpoch=${epoch} currentEpoch=${autoCapture.epoch} doneSent=${autoCapture.episodeDoneSent}`);
    return;
  }
  autoCapture.episodeDoneSent = true;

  // Notify the content script that this episode is done
  autoCapture.doneCount++;
  updateBadge();

  console.log(`[AC] autoConfirmCapture DONE-SIGNAL ep=${ep} epoch=${epoch}`);
  notifyTab(tabId, {
    type: "autoCaptureEpisodeDone",
    season: autoCapture.multiSeason ? autoCapture.currentSeason : autoCapture.season,
    episode: autoCapture.currentEp,
  });
}

// ========= BROCOFLIX BROWSER-SIDE DOWNLOAD =========

// Track active BrocoFlix downloads: sessionId -> { tabId, frameId, epKey }
const brocoflixSessions = new Map();
// Dedup: prevent multiple startBrocoflixDownload calls for the same m3u8 URL
const brocoflixActiveUrls = new Set();
// Temporarily holds frameId between webRequest callback and pending object creation
let brocoflixPendingFrameId = null;
// Track reload-recovery state: when the iframe is reloaded to get a fresh connection pool,
// this holds the completed segment indices so the re-injected downloader can skip them.
// Key: sessionId, Value: { tabId, completedIndices: Set<int>, totalSegments, pageUrl,
//                          epKey, resolve: fn(m3u8Url, frameId) }
const brocoflixReloadState = new Map();
// Max segments to download before proactively reloading the iframe to avoid HTTP/2 GOAWAY
const BROCOFLIX_RELOAD_THRESHOLD = 800;

function cleanupBrocoflixSession(sessionId) {
  const session = brocoflixSessions.get(sessionId);
  if (session?.m3u8Url) brocoflixActiveUrls.delete(session.m3u8Url);
  brocoflixSessions.delete(sessionId);
}

async function startBrocoflixDownload(m3u8Url, pageUrl, tabId, frameId, resumeOpts) {
  // resumeOpts: { sessionId, completedIndices: number[] } when resuming after iframe reload
  // Client-side dedup: skip if this exact URL is already being downloaded
  // (but allow through if this is a resume after iframe reload)
  if (!resumeOpts && brocoflixActiveUrls.has(m3u8Url)) {
    console.log(`[BF] Skipping duplicate m3u8 (already in progress): ${m3u8Url.slice(0, 80)}`);
    return;
  }
  if (!resumeOpts) brocoflixActiveUrls.add(m3u8Url);

  let epCtx = episodeContextByTab.get(tabId);

  // If no episode context (e.g. movies), query the page DOM for the title
  if (!epCtx || !epCtx.show_name) {
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
          const h1 = document.querySelector("#details-container h1");
          return h1?.textContent?.trim() || document.title.replace(/\s*[\|–\-].*$/, "").trim();
        },
      });
      const title = results?.[0]?.result || "";
      if (title) {
        epCtx = { show_name: title, season: null, episode: null };
        episodeContextByTab.set(tabId, epCtx);
      }
    } catch (err) {
      console.log(`[BF] Failed to get page title: ${err.message}`);
    }
  }

  epCtx = epCtx || {};

  let sessionId, epKey;

  if (resumeOpts) {
    // Resuming after iframe reload — reuse existing session
    sessionId = resumeOpts.sessionId;
    const session = brocoflixSessions.get(sessionId);
    epKey = session?.epKey || "";
    // Update frameId since the iframe was reloaded
    if (session) session.frameId = frameId;
    console.log(`[BF] Resuming download after iframe reload: sessionId=${sessionId} frameId=${frameId} completed=${resumeOpts.completedIndices.length} segments`);
  } else {
    // Fresh download — create new session on server
    console.log(`[BF] Starting download: frameId=${frameId} show="${epCtx.show_name}" S${epCtx.season}E${epCtx.episode}`);

    let startData;
    try {
      const resp = await fetch("http://localhost:9876/brocoflix-start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          m3u8_url: m3u8Url,
          page_url: pageUrl,
          show_name: epCtx.show_name || "",
          season: epCtx.season ?? null,
          episode: epCtx.episode ?? null,
        }),
      });
      startData = await resp.json();
    } catch (err) {
      console.log(`[BF] Server not running: ${err.message}`);
      brocoflixActiveUrls.delete(m3u8Url);
      return;
    }

    if (startData.status !== "ok") {
      console.log(`[BF] Start rejected: ${startData.status} — ${startData.message}`);
      brocoflixActiveUrls.delete(m3u8Url);
      return;
    }

    sessionId = startData.session_id;
    epKey = startData.ep_key;
    console.log(`[BF] Session created: ${sessionId} → ${startData.filename}`);

    brocoflixSessions.set(sessionId, { tabId, frameId, epKey, m3u8Url, pageUrl });

    // Register in captures for popup display
    const capture = {
      m3u8_url: m3u8Url,
      page_url: pageUrl,
      tabId,
      timestamp: Date.now(),
      status: "uploading",
      message: startData.filename,
    };
    captures.push(capture);
    if (captures.length > 50) captures = captures.slice(-50);
    updateBadge();
  }

  const completedIndices = resumeOpts?.completedIndices || [];

  // Inject the MAIN-world downloader into the embed iframe
  chrome.scripting.executeScript({
    target: { tabId, frameIds: [frameId] },
    world: "MAIN",
    func: brocoflixDownloaderFunc,
    args: [m3u8Url, sessionId, completedIndices, BROCOFLIX_RELOAD_THRESHOLD],
  }).catch(err => {
    console.log(`[BF] Injection failed: ${err.message}`);
    if (!resumeOpts) cleanupBrocoflixSession(sessionId);
  });
}

// This function is injected into the embed iframe's MAIN world.
// It fetches the m3u8 manifest, downloads TS segments, and posts them
// to the content script relay via window.postMessage.
//
// Reload-recovery strategy: instead of retrying failed segments on the same
// connection (which never works due to HTTP/2 GOAWAY / Chrome socket pool
// poisoning), the downloader stops on first failure and asks the background
// to reload the iframe for a fresh CDN domain + connection pool. It also
// proactively requests a reload every `reloadThreshold` segments to stay
// under the GOAWAY limit.
function brocoflixDownloaderFunc(m3u8Url, sessionId, completedIndices, reloadThreshold) {
  const alreadyDone = new Set(completedIndices || []);

  async function blobToBase64(blob) {
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onloadend = () => resolve(reader.result.split(",")[1]);
      reader.readAsDataURL(blob);
    });
  }

  // Fetch with AbortController timeout
  async function fetchWithTimeout(url, timeoutMs = 30000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const resp = await fetch(url, { signal: controller.signal, cache: "no-store" });
      clearTimeout(timer);
      return resp;
    } catch (err) {
      clearTimeout(timer);
      throw err;
    }
  }

  async function fetchManifestSegments() {
    const resp = await fetchWithTimeout(m3u8Url);
    if (!resp.ok) throw new Error(`m3u8 fetch failed: ${resp.status}`);
    const manifest = await resp.text();

    let mediaManifest = manifest;
    let baseUrl = m3u8Url;

    if (manifest.includes("#EXT-X-STREAM-INF")) {
      const lines = manifest.split("\n");
      let bestBw = -1;
      let bestUrl = null;
      for (let i = 0; i < lines.length; i++) {
        const m = lines[i].match(/#EXT-X-STREAM-INF:.*BANDWIDTH=(\d+)/);
        if (m && i + 1 < lines.length) {
          const bw = parseInt(m[1], 10);
          if (bw > bestBw) {
            bestBw = bw;
            bestUrl = lines[i + 1].trim();
          }
        }
      }
      if (!bestUrl) throw new Error("No variants found in master playlist");

      const variantUrl = bestUrl.startsWith("http")
        ? bestUrl
        : new URL(bestUrl, m3u8Url).href;

      const varResp = await fetchWithTimeout(variantUrl);
      if (!varResp.ok) throw new Error(`Variant fetch failed: ${varResp.status}`);
      mediaManifest = await varResp.text();
      baseUrl = variantUrl;
    }

    return mediaManifest
      .split("\n")
      .filter((line) => line.trim() && !line.startsWith("#"))
      .map((line) =>
        line.trim().startsWith("http")
          ? line.trim()
          : new URL(line.trim(), baseUrl).href
      );
  }

  // Relay log messages to background via content script so they appear
  // in the service worker console (iframe console is inaccessible on BrocoFlix)
  function relayLog(msg) {
    console.log(msg);
    window.postMessage({ type: "hlsBrocoLog", sessionId, msg }, "*");
  }

  async function run() {
    try {
      // Pause the video player so it stops competing for CDN bandwidth.
      try {
        const videos = document.querySelectorAll("video");
        videos.forEach(v => { v.pause(); v.src = ""; v.load(); });
        if (window.jwplayer) try { window.jwplayer().remove(); } catch {}
        if (window.hls) try { window.hls.destroy(); } catch {}
        if (window.player?.destroy) try { window.player.destroy(); } catch {}
        relayLog(`[BF-dl] Paused/destroyed ${videos.length} video element(s)`);
      } catch (e) {
        relayLog(`[BF-dl] Video pause failed (non-fatal): ${e.message}`);
      }

      const segments = await fetchManifestSegments();
      if (segments.length === 0) throw new Error("No segments found in m3u8");

      const totalSegs = segments.length;
      const remaining = totalSegs - alreadyDone.size;

      // Retry pass: only fetch missing segments with longer delays between attempts
      const isRetryPass = remaining <= 20 && alreadyDone.size > 0;
      const THROTTLE_MS = isRetryPass ? 10000 : 500;
      const MAX_CONSECUTIVE_FAILS = isRetryPass ? remaining + 1 : 10;

      relayLog(`[BF-dl] ${totalSegs} segments in manifest, ${alreadyDone.size} already done, ${remaining} remaining${isRetryPass ? ` (RETRY PASS: ${THROTTLE_MS/1000}s between fetches)` : ""}`);

      window.postMessage(
        { type: "hlsBrocoStart", sessionId, totalChunks: totalSegs },
        "*"
      );
      let segsFetchedThisCycle = 0;
      let consecutiveFails = 0;
      const failedThisCycle = []; // segment indices that failed this cycle
      const allCompleted = new Set(alreadyDone);

      // Server appends chunks in receive order, so we send in segment-index order.
      // Buffer downloaded data past any gap until the gap is filled.
      const downloaded = new Array(totalSegs).fill(null);
      let sendCursor = 0;

      // Advance sendCursor past segments already sent in prior cycles.
      while (sendCursor < totalSegs && alreadyDone.has(sendCursor)) {
        sendCursor++;
      }

      function flushToServer() {
        while (sendCursor < totalSegs) {
          if (alreadyDone.has(sendCursor)) {
            sendCursor++;
            continue;
          }
          if (downloaded[sendCursor] === null) break;
          window.postMessage(
            {
              type: "hlsBrocoChunk",
              sessionId,
              chunkIndex: sendCursor,
              totalChunks: totalSegs,
              data: downloaded[sendCursor],
            },
            "*"
          );
          downloaded[sendCursor] = null; // free memory
          sendCursor++;
        }
      }

      let needsReload = false;
      let reloadReason = "";

      for (let segIdx = 0; segIdx < totalSegs; segIdx++) {
        if (alreadyDone.has(segIdx)) continue;

        // Proactive reload before GOAWAY threshold
        if (reloadThreshold > 0 && segsFetchedThisCycle >= reloadThreshold) {
          flushToServer();
          needsReload = true;
          reloadReason = "proactive";
          relayLog(`[BF-dl] Proactive reload after ${segsFetchedThisCycle} segments. ${allCompleted.size}/${totalSegs} total done, ${failedThisCycle.length} skipped.`);
          break;
        }

        let ok = false;
        try {
          const segResp = await fetchWithTimeout(segments[segIdx]);
          if (!segResp.ok) throw new Error(`HTTP ${segResp.status}`);
          const blob = await segResp.blob();
          downloaded[segIdx] = await blobToBase64(blob);
          ok = true;
        } catch (err) {
          relayLog(`[BF-dl] Segment ${segIdx} FAILED: ${err.message} — skipping`);
        }

        if (ok) {
          segsFetchedThisCycle++;
          consecutiveFails = 0;
          allCompleted.add(segIdx);
          flushToServer();

          if (allCompleted.size % 50 === 0) {
            relayLog(`[BF-dl] Progress: ${allCompleted.size}/${totalSegs} done (cycle: ${segsFetchedThisCycle}, skipped: ${failedThisCycle.length})`);
          }
        } else {
          failedThisCycle.push(segIdx);
          consecutiveFails++;

          // If many consecutive segments fail, the connection is dead — stop early
          if (consecutiveFails >= MAX_CONSECUTIVE_FAILS) {
            flushToServer();
            needsReload = true;
            reloadReason = "consecutive_fails";
            relayLog(`[BF-dl] ${MAX_CONSECUTIVE_FAILS} consecutive failures — connection dead. ${allCompleted.size}/${totalSegs} done, ${failedThisCycle.length} skipped. Requesting reload...`);
            break;
          }
        }

        await new Promise(r => setTimeout(r, THROTTLE_MS));
      }

      flushToServer();

      // If we have failed segments or need a proactive reload, request one
      if (!needsReload && failedThisCycle.length > 0) {
        needsReload = true;
        reloadReason = "retry_failures";
        relayLog(`[BF-dl] Pass complete. ${allCompleted.size}/${totalSegs} done, ${failedThisCycle.length} failed: [${failedThisCycle.join(",")}]. Requesting reload to retry...`);
      }

      if (needsReload) {
        window.postMessage({
          type: "hlsBrocoNeedReload",
          sessionId,
          completedIndices: [...allCompleted],
          totalSegments: totalSegs,
          reason: reloadReason,
        }, "*");
        return; // background will reload iframe and re-inject us
      }

      // All segments downloaded!
      if (sendCursor < totalSegs) {
        throw new Error(`Only ${sendCursor}/${totalSegs} segments sent to server`);
      }

      relayLog(`[BF-dl] All ${totalSegs} segments complete!`);
      window.postMessage({ type: "hlsBrocoDone", sessionId }, "*");
    } catch (err) {
      window.postMessage(
        { type: "hlsBrocoError", sessionId, error: err.message },
        "*"
      );
    }
  }

  run();
}

function startAutoCapture(params, tabId, tabUrl) {
  const siteConfig = getSiteConfig(tabUrl);
  const serverNum = params.serverNum || 1;

  if (params.multiSeason) {
    // Multi-season mode: start at first season, ep 1, endEp unknown (detected per season)
    autoCapture = {
      active: true,
      finished: false,
      tabId,
      season: params.startSeason,
      startEp: 1,
      endEp: null,       // set per-season by autoCaptureEpisodesDiscovered or autoCaptureSeasonDetected
      currentEp: 1,
      doneCount: 0,
      totalCount: 0,     // accumulated as seasons are detected/discovered
      siteConfig,
      serverNum,
      epoch: 0,
      episodeDoneSent: false,
      graceUntil: 0,
      multiSeason: true,
      startSeason: params.startSeason,
      endSeason: params.endSeason,
      currentSeason: params.startSeason,
      consecutiveSkips: 0,
      episodeHashes: [],
    };
  } else {
    // Single-season episode-range mode (existing behavior)
    autoCapture = {
      active: true,
      finished: false,
      tabId,
      season: params.season,
      startEp: params.startEp,
      endEp: params.endEp,
      currentEp: params.startEp,
      doneCount: 0,
      totalCount: params.endEp - params.startEp + 1,
      siteConfig,
      serverNum,
      epoch: 0,
      episodeDoneSent: false,
      graceUntil: 0,
      multiSeason: false,
      startSeason: null,
      endSeason: null,
      currentSeason: null,
      episodeHashes: [],
    };
  }

  // Clear seen state so first episode's m3u8 and subtitles are detected fresh
  seenM3u8.clear();
  subtitlesSent.clear();
  pendingCaptures = [];
  updateBadge();

  // Navigate to the first episode
  if (siteConfig?.navStrategy === "hash-reload") {
    // For hash-reload sites, navigate directly via scripting API.
    // This is more reliable than notifyTab because it doesn't depend on
    // an existing content script (which may have a stale context after
    // extension reload).  After reload, the fresh content script picks up
    // auto-capture state via checkAutoCaptureOnLoad.
    const hash = siteConfig.makeEpisodeHash(autoCapture.season, autoCapture.startEp);
    chrome.scripting.executeScript({
      target: { tabId },
      func: (h) => { location.hash = h; location.reload(); },
      args: [hash],
    });
  } else {
    // SPA sites: content script handles navigation in-place
    notifyTab(tabId, {
      type: "beginAutoCapture",
      season: autoCapture.season,
      startEp: autoCapture.startEp,
      endEp: autoCapture.endEp,
      siteConfig,
      serverNum,
      multiSeason: autoCapture.multiSeason,
    });
  }
}

function stopAutoCapture() {
  const tabId = autoCapture.tabId;
  autoCapture.active = false;
  autoCapture.finished = false;
  updateBadge();

  if (tabId) {
    notifyTab(tabId, { type: "stopAutoCapture" });
  }
}

// ========= MESSAGE HANDLER =========

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "getCaptures") {
    sendResponse({ captures, pendingCaptures });
  } else if (msg.type === "confirmDownload") {
    confirmDownload(msg.index).then(() => sendResponse({ ok: true }));
    return true;  // async response
  } else if (msg.type === "dismissCapture") {
    dismissCapture(msg.index);
    sendResponse({ ok: true });
  } else if (msg.type === "clearCaptures") {
    captures = [];
    pendingCaptures = [];
    subtitlesSent.clear();
    seenM3u8.clear();
    episodeContextByTab.clear();
    autoCapture = {
      active: false, finished: false, tabId: null, season: null,
      startEp: null, endEp: null, currentEp: null, doneCount: 0,
      totalCount: 0, siteConfig: null, graceUntil: 0,
      epoch: 0, episodeDoneSent: false,
      multiSeason: false, startSeason: null, endSeason: null, currentSeason: null,
      consecutiveSkips: 0, episodeHashes: [],
    };
    updateBadge();
    sendResponse({ ok: true });
  } else if (msg.type === "startAutoCapture") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs[0];
      if (!tab) {
        sendResponse({ ok: false, error: "No active tab" });
        return;
      }
      startAutoCapture(msg, tab.id, tab.url);
      sendResponse({ ok: true });
    });
    return true; // async response
  } else if (msg.type === "stopAutoCapture") {
    stopAutoCapture();
    sendResponse({ ok: true });
  } else if (msg.type === "getAutoCaptureState") {
    sendResponse({
      active: autoCapture.active,
      finished: autoCapture.finished,
      season: autoCapture.season,
      startEp: autoCapture.startEp,
      endEp: autoCapture.endEp,
      currentEp: autoCapture.currentEp,
      doneCount: autoCapture.doneCount,
      totalCount: autoCapture.totalCount,
      multiSeason: autoCapture.multiSeason,
      startSeason: autoCapture.startSeason,
      endSeason: autoCapture.endSeason,
      currentSeason: autoCapture.currentSeason,
    });
  } else if (msg.type === "checkAutoCapture") {
    // Content script checks on page load if auto-capture is active for this tab
    if (autoCapture.active && sender.tab && sender.tab.id === autoCapture.tabId) {
      // Provide the discovered hash for the current episode (if available)
      const epIndex = autoCapture.currentEp - 1;
      const currentHash = autoCapture.episodeHashes.length > 0 && epIndex >= 0 && epIndex < autoCapture.episodeHashes.length
        ? autoCapture.episodeHashes[epIndex].hash
        : null;
      sendResponse({
        active: true,
        season: autoCapture.season,
        currentEp: autoCapture.currentEp,
        startEp: autoCapture.startEp,
        endEp: autoCapture.endEp,
        siteConfig: autoCapture.siteConfig,
        serverNum: autoCapture.serverNum,
        multiSeason: autoCapture.multiSeason,
        currentSeason: autoCapture.currentSeason,
        startSeason: autoCapture.startSeason,
        endSeason: autoCapture.endSeason,
        needsDiscovery: autoCapture.episodeHashes.length === 0,
        hash: currentHash,
      });
    } else {
      sendResponse({ active: false });
    }
  } else if (msg.type === "autoCaptureAdvance") {
    // Content script finished an episode, advance to next or complete
    const skipped = msg.skipped === true;

    // Multi-season with unknown episode count (hash-reload): use consecutive
    // skip counter — require 2 consecutive skipped episodes before declaring
    // a season done.  A single transient failure (slow page load, timing) won't
    // prematurely end the capture.
    if (skipped && autoCapture.active && autoCapture.multiSeason && autoCapture.endEp == null) {
      autoCapture.consecutiveSkips = (autoCapture.consecutiveSkips || 0) + 1;
      console.log(`[AC] SKIP S${autoCapture.currentSeason} EP${autoCapture.currentEp} (consecutive: ${autoCapture.consecutiveSkips})`);

      if (autoCapture.consecutiveSkips >= 2) {
        // 2 consecutive skips = season confirmed done
        autoCapture.consecutiveSkips = 0;
        if (autoCapture.currentSeason < autoCapture.endSeason) {
          // Advance to next season
          const prevSeason = autoCapture.currentSeason;
          autoCapture.currentSeason++;
          autoCapture.season = autoCapture.currentSeason;
          autoCapture.currentEp = 1;
          autoCapture.startEp = 1;
          autoCapture.endEp = null;
          autoCapture.episodeHashes = [];  // clear so content script re-discovers
          autoCapture.epoch++;
          autoCapture.episodeDoneSent = false;
          console.log(`[AC] SKIP->ADVANCE SEASON S${prevSeason} -> S${autoCapture.currentSeason} (epoch ${autoCapture.epoch})`);
          updateBadge();
          sendResponse({
            hasNext: true,
            season: autoCapture.currentSeason,
            nextEp: 1,
            startEp: 1,
            endEp: null,
            siteConfig: autoCapture.siteConfig,
            serverNum: autoCapture.serverNum,
            multiSeason: true,
            newSeason: true,
          });
        } else {
          // Last season done
          console.log(`[AC] SKIP on last season S${autoCapture.currentSeason} — finishing`);
          autoCapture.active = false;
          autoCapture.finished = true;
          autoCapture.graceUntil = Date.now() + 15000;
          updateBadge();
          sendResponse({ hasNext: false });
        }
      } else {
        // First skip — try next episode (might be a transient failure)
        const prevEp = autoCapture.currentEp;
        autoCapture.currentEp++;
        autoCapture.epoch++;
        autoCapture.episodeDoneSent = false;
        console.log(`[AC] SKIP->TRY NEXT ep ${prevEp} -> ${autoCapture.currentEp} (epoch ${autoCapture.epoch})`);
        updateBadge();
        const skipNextIdx = autoCapture.currentEp - 1;
        const skipNextHash = autoCapture.episodeHashes.length > skipNextIdx ? autoCapture.episodeHashes[skipNextIdx].hash : null;
        sendResponse({
          hasNext: true,
          season: autoCapture.multiSeason ? autoCapture.currentSeason : autoCapture.season,
          nextEp: autoCapture.currentEp,
          startEp: autoCapture.startEp,
          endEp: autoCapture.endEp,
          siteConfig: autoCapture.siteConfig,
          serverNum: autoCapture.serverNum,
          multiSeason: autoCapture.multiSeason,
          hash: skipNextHash,
        });
      }
    } else if (autoCapture.active && (autoCapture.endEp == null || autoCapture.currentEp < autoCapture.endEp)) {
      // Next episode in current season (endEp null = unknown count, keep going)
      if (!skipped) autoCapture.consecutiveSkips = 0;  // reset on successful capture
      const prevEp = autoCapture.currentEp;
      autoCapture.currentEp++;
      autoCapture.epoch++;
      autoCapture.episodeDoneSent = false;
      console.log(`[AC] ADVANCE ep ${prevEp} -> ${autoCapture.currentEp} (epoch ${autoCapture.epoch})`);
      updateBadge();
      const advNextIdx = autoCapture.currentEp - 1;
      const advNextHash = autoCapture.episodeHashes.length > advNextIdx ? autoCapture.episodeHashes[advNextIdx].hash : null;
      sendResponse({
        hasNext: true,
        season: autoCapture.multiSeason ? autoCapture.currentSeason : autoCapture.season,
        nextEp: autoCapture.currentEp,
        startEp: autoCapture.startEp,
        endEp: autoCapture.endEp,
        siteConfig: autoCapture.siteConfig,
        serverNum: autoCapture.serverNum,
        multiSeason: autoCapture.multiSeason,
        hash: advNextHash,
      });
    } else if (autoCapture.active && autoCapture.multiSeason && autoCapture.currentSeason < autoCapture.endSeason) {
      // Multi-season: advance to next season (endEp reached)
      const prevSeason = autoCapture.currentSeason;
      autoCapture.currentSeason++;
      autoCapture.season = autoCapture.currentSeason;
      autoCapture.currentEp = 1;
      autoCapture.startEp = 1;
      autoCapture.endEp = null;  // will be set by autoCaptureEpisodesDiscovered or autoCaptureSeasonDetected
      autoCapture.episodeHashes = [];  // clear so content script re-discovers
      autoCapture.epoch++;
      autoCapture.episodeDoneSent = false;
      console.log(`[AC] ADVANCE SEASON S${prevSeason} -> S${autoCapture.currentSeason} (epoch ${autoCapture.epoch})`);
      updateBadge();
      sendResponse({
        hasNext: true,
        season: autoCapture.currentSeason,
        nextEp: 1,
        startEp: 1,
        endEp: null,
        siteConfig: autoCapture.siteConfig,
        serverNum: autoCapture.serverNum,
        multiSeason: true,
        newSeason: true,
      });
    } else {
      autoCapture.active = false;
      autoCapture.finished = true;
      // Grace period: keep auto-confirming for 15s in case the last episode's
      // m3u8 fires after the content script already timed out and advanced.
      autoCapture.graceUntil = Date.now() + 15000;
      updateBadge();
      sendResponse({ hasNext: false });
    }
  } else if (msg.type === "autoCaptureClickedPlay") {
    // Content script tells us it clicked play for an episode — update current ep
    autoCapture.currentEp = msg.episode;
    updateBadge();
    sendResponse({ ok: true });
  } else if (msg.type === "autoCaptureComplete") {
    autoCapture.active = false;
    autoCapture.finished = true;
    updateBadge();
    sendResponse({ ok: true });
  } else if (msg.type === "clearEpisodeState") {
    // Clear seen m3u8s between episodes so the next capture is detected fresh
    console.log(`[AC] clearEpisodeState (ep ${autoCapture.currentEp}, epoch ${autoCapture.epoch}, had ${seenM3u8.size} seen urls)`);
    seenM3u8.clear();
    subtitlesSent.clear();
    autoCapture.episodeDoneSent = false;  // allow retry to receive a fresh done-signal
    sendResponse({ ok: true });
  } else if (msg.type === "autoCaptureEpisodesDiscovered") {
    // Content script discovered episode hashes from the DOM (hash-reload sites)
    if (autoCapture.active && msg.episodes && msg.episodes.length > 0) {
      autoCapture.episodeHashes = msg.episodes;
      autoCapture.endEp = msg.episodes.length;
      autoCapture.startEp = 1;
      autoCapture.totalCount += msg.episodes.length;
      const season = autoCapture.multiSeason ? autoCapture.currentSeason : autoCapture.season;
      console.log(`[AC] Season S${season}: discovered ${msg.episodes.length} episodes`);
      updateBadge();

      // Notify the server so it can log the season episode list
      fetch(SEASON_INFO_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          show_name: msg.showName || "",
          season,
          episodes: msg.episodes,
        }),
      }).catch(() => {}); // fire-and-forget
    }
    sendResponse({ ok: true });
  } else if (msg.type === "autoCaptureSeasonDetected") {
    // Content script reports episode count after selecting a season in the dropdown
    if (autoCapture.active && autoCapture.multiSeason) {
      autoCapture.endEp = msg.episodeCount;
      autoCapture.startEp = 1;
      autoCapture.totalCount += msg.episodeCount;
      console.log(`[AC] Season S${autoCapture.currentSeason} detected: ${msg.episodeCount} episodes (total across seasons: ${autoCapture.totalCount})`);
      updateBadge();
    }
    sendResponse({ ok: true });
  } else if (msg.type === "brocoflixDiag") {
    console.log(`[BF] diagnostic: ${msg.info}`);
    sendResponse({ ok: true });
  } else if (msg.type === "brocoflixLog") {
    // Relayed log from MAIN-world downloader in iframe
    console.log(msg.msg);
    sendResponse({ ok: true });
  } else if (msg.type === "brocoflixChunkData") {
    // Content script relayed a chunk from MAIN-world — POST binary to server
    const { sessionId, chunkIndex, totalChunks, data } = msg;

    (async () => {
      try {
        // Decode base64 to binary
        const binaryStr = atob(data);
        const bytes = new Uint8Array(binaryStr.length);
        for (let i = 0; i < binaryStr.length; i++) {
          bytes[i] = binaryStr.charCodeAt(i);
        }

        await fetch("http://localhost:9876/brocoflix-chunk", {
          method: "POST",
          headers: {
            "Content-Type": "application/octet-stream",
            "X-Session-Id": sessionId,
            "X-Chunk-Index": String(chunkIndex),
            "X-Total-Chunks": String(totalChunks),
          },
          body: bytes.buffer,
        });

        if (chunkIndex % 50 === 0 || chunkIndex === totalChunks - 1) {
          console.log(`[BF] chunk ${chunkIndex + 1}/${totalChunks}`);
        }
      } catch (err) {
        console.log(`[BF] chunk POST failed: ${err.message}`);
      }
      sendResponse({ ok: true });
    })();
    return true; // async response
  } else if (msg.type === "brocoflixDoneSignal") {
    const { sessionId } = msg;
    const session = brocoflixSessions.get(sessionId);
    const epKey = session?.epKey || "";

    console.log(`[BF] All chunks received, requesting mux...`);

    // Tell server to mux TS -> MP4
    (async () => {
      try {
        const resp = await fetch("http://localhost:9876/brocoflix-done", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, ep_key: epKey }),
        });
        const result = await resp.json();
        console.log(`[BF] Mux result: ${result.status} ${result.message || result.filename || ""}`);
      } catch (err) {
        console.log(`[BF] Mux request failed: ${err.message}`);
      }

      cleanupBrocoflixSession(sessionId);

      // If auto-capture is active for this tab, fire done-signal
      const senderTabId = sender.tab?.id;
      if (autoCapture.active && senderTabId === autoCapture.tabId && !autoCapture.episodeDoneSent) {
        autoCapture.episodeDoneSent = true;
        autoCapture.doneCount++;
        updateBadge();
        console.log(`[BF] Auto-capture done-signal for ep ${autoCapture.currentEp}`);
        notifyTab(senderTabId, {
          type: "autoCaptureEpisodeDone",
          season: autoCapture.multiSeason ? autoCapture.currentSeason : autoCapture.season,
          episode: autoCapture.currentEp,
        });
      }
      sendResponse({ ok: true });
    })();
    return true; // async response
  } else if (msg.type === "brocoflixNeedReload") {
    // MAIN-world downloader hit a failure or proactive threshold — needs fresh connection pool.
    // Reload the embed iframe to get a new CDN domain, then re-inject with the skip list.
    const { sessionId, completedIndices, totalSegments, reason } = msg;
    const session = brocoflixSessions.get(sessionId);
    if (!session) {
      console.log(`[BF] Reload requested for unknown session ${sessionId}`);
      sendResponse({ ok: false });
      return;
    }

    const tabId = session.tabId;
    const pageUrl = session.pageUrl;

    // Track reload count and detect if we're stuck (no progress between reloads)
    session.reloadCount = (session.reloadCount || 0) + 1;
    const prevCompleted = session.lastCompletedCount || 0;
    session.lastCompletedCount = completedIndices.length;
    const madeProgress = completedIndices.length > prevCompleted;
    const MAX_NO_PROGRESS_RELOADS = 3;
    const MAX_TOTAL_RELOADS = 15; // hard limit to prevent infinite loops

    if (!madeProgress) {
      session.noProgressReloads = (session.noProgressReloads || 0) + 1;
    } else {
      session.noProgressReloads = 0;
    }

    const completionPct = (completedIndices.length / totalSegments * 100).toFixed(1);
    const missing = totalSegments - completedIndices.length;
    const TARGET_PCT = 99.5;
    const meetsTarget = parseFloat(completionPct) >= TARGET_PCT;

    console.log(`[BF] Iframe reload #${session.reloadCount} (${reason}): ${completedIndices.length}/${totalSegments} (${completionPct}%) done, ${missing} missing, target=${TARGET_PCT}%, progress=${madeProgress}, stalled=${session.noProgressReloads}/${MAX_NO_PROGRESS_RELOADS}`);

    // Give up if:
    // 1. Meets 99.5% target AND stalled — we're done, mux it
    // 2. Below target but stalled for 5+ reloads — segments are permanently blocked by CDN
    // 3. Hit hard reload limit
    const shouldGiveUp = (meetsTarget && session.noProgressReloads >= MAX_NO_PROGRESS_RELOADS)
      || session.noProgressReloads >= MAX_NO_PROGRESS_RELOADS + 2
      || session.reloadCount >= MAX_TOTAL_RELOADS;
    if (shouldGiveUp) {
      const reason2 = session.reloadCount >= MAX_TOTAL_RELOADS
        ? `Hit max ${MAX_TOTAL_RELOADS} reloads`
        : meetsTarget
          ? `Reached ${completionPct}% (>= ${TARGET_PCT}% target) and stalled`
          : `Stalled at ${completionPct}% (${missing} segments permanently blocked by CDN) — best achievable`;
      console.log(`[BF] ${reason2}. Finishing with ${missing} segments missing.`);
      // Tell the MAIN-world we're done (skip missing segments)
      // Post a done signal — the server will mux whatever chunks we sent
      fetch("http://localhost:9876/brocoflix-done", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, ep_key: session.epKey }),
      }).then(r => r.json()).then(result => {
        console.log(`[BF] Forced mux result (${missing} segments missing): ${result.status} ${result.message || result.filename || ""}`);
      }).catch(err => {
        console.log(`[BF] Forced mux failed: ${err.message}`);
      });
      cleanupBrocoflixSession(sessionId);
      sendResponse({ ok: true });
      return;
    }

    // Store the reload state so we can resume when the new m3u8 is intercepted
    brocoflixReloadState.set(sessionId, {
      tabId,
      completedIndices,
      totalSegments,
      pageUrl,
    });

    // Cancel any previous reload timer for this session before setting a new one.
    // Without this, a timer from reload #N can fire during reload #N+1's retry pass
    // (which takes ~90s for 9 segments × 10s) and abort the session prematurely.
    const session2 = brocoflixSessions.get(sessionId);
    if (session2?._reloadTimer) clearTimeout(session2._reloadTimer);
    const reloadTimer = setTimeout(() => {
      if (brocoflixReloadState.has(sessionId)) {
        console.log(`[BF] Reload timeout for session ${sessionId} — no new m3u8 after 120s. Aborting.`);
        brocoflixReloadState.delete(sessionId);
        fetch("http://localhost:9876/brocoflix-abort", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId }),
        }).catch(() => {});
        cleanupBrocoflixSession(sessionId);
      }
    }, 120000);
    if (session2) session2._reloadTimer = reloadTimer;

    // Allow the new m3u8 to be intercepted (clear dedup)
    seenM3u8.clear();

    // Reload ONLY the embed iframe (not the whole page, which would lose the player).
    // Clearing the iframe src and restoring it forces a fresh load with new CDN domain
    // and a clean Chrome socket pool. Then auto-click the play button inside the iframe
    // so the HLS player loads and fires the m3u8 request without manual intervention.
    chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const iframe = document.querySelector("iframe");
        if (!iframe || !iframe.src) return "no_iframe";
        let src = iframe.src;
        // Force autoPlay so the player starts without a manual click
        src = src.replace("autoPlay=false", "autoPlay=true");
        iframe.src = "";
        // Small delay to ensure the old connection pool is torn down
        setTimeout(() => { iframe.src = src; }, 500);
        return "ok";
      },
    }).then(results => {
      const r = results?.[0]?.result;
      if (r === "ok") {
        console.log(`[BF] Embed iframe reloaded. Will auto-click play button in iframe...`);
        // Wait for iframe to load, then inject into ALL frames (allFrames: true)
        // to avoid needing webNavigation permission. The injected function
        // checks if it's in the right frame before clicking.
        setTimeout(() => {
          chrome.scripting.executeScript({
            target: { tabId, allFrames: true },
            world: "MAIN",
            func: () => {
              // Only act inside the embed iframe, not the top-level BrocoFlix page
              if (window === window.top) return null;
              function tryClick() {
                const selectors = [
                  "#btn-play", ".play-button", ".jw-icon-display", ".vjs-big-play-button",
                  "[aria-label='Play']", "button.player-btn", ".plyr__control--overlaid",
                  ".play-overlay", ".play_btn", "#play-btn",
                ];
                for (const sel of selectors) {
                  const el = document.querySelector(sel);
                  if (el) { el.click(); return sel; }
                }
                const video = document.querySelector("video");
                if (video) { video.click(); return "video"; }
                return null;
              }
              let clicked = tryClick();
              if (!clicked) {
                let attempts = 0;
                const timer = setInterval(() => {
                  clicked = tryClick();
                  attempts++;
                  if (clicked || attempts >= 10) clearInterval(timer);
                }, 500);
              }
              return clicked;
            },
          }).then(results => {
            const clicks = results?.filter(r => r.result).map(r => r.result);
            console.log(`[BF] Auto-play click results: ${clicks.length ? clicks.join(", ") : "retrying via interval..."}`);
          }).catch(err => {
            console.log(`[BF] Auto-play inject failed: ${err.message}`);
          });
        }, 3000); // wait 3s for iframe to fully load
      } else {
        console.log(`[BF] Iframe reload problem: ${r}. Falling back to full tab reload.`);
        chrome.tabs.reload(tabId);
      }
    }).catch(err => {
      console.log(`[BF] Iframe reload script failed: ${err.message}. Falling back to full tab reload.`);
      chrome.tabs.reload(tabId);
    });
    sendResponse({ ok: true });
  } else if (msg.type === "brocoflixErrorSignal") {
    const { sessionId, error } = msg;
    console.log(`[BF] Download FAILED: ${error}`);

    // Tell server to abort
    fetch("http://localhost:9876/brocoflix-abort", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    }).catch(() => {});
    cleanupBrocoflixSession(sessionId);
    sendResponse({ ok: true });
  } else if (msg.type === "setEpisodeContext") {
    // Content script reports which episode is playing (for DOM-based sites like brocoflix)
    const tabId = sender.tab?.id;
    console.log(`[HLS] setEpisodeContext received: tab=${tabId} show="${msg.show_name}" S${msg.season}E${msg.episode}`);
    if (tabId != null) {
      episodeContextByTab.set(tabId, {
        show_name: msg.show_name || "",
        season: msg.season ?? null,
        episode: msg.episode ?? null,
      });
    }
    sendResponse({ ok: true });
  }
  return true;
});
