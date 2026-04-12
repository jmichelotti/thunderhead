// HLS Capture - In-page confirmation dialog
// Injected into every page; listens for messages from background.js

const DIALOG_ID = "hls-capture-dialog-container";

// Manifest has all_frames: true so the BrocoFlix chunk relay (below) can run
// inside the video iframe.  But the auto-capture loop and dialog UI must run
// in the top frame only — otherwise each frame reacts to autoCaptureEpisodeDone
// and sends its own autoCaptureAdvance, double-incrementing currentEp and
// skipping every other episode.
const IS_TOP_FRAME = window.top === window;

function getOrCreateContainer() {
  let container = document.getElementById(DIALOG_ID);
  if (!container) {
    container = document.createElement("div");
    container.id = DIALOG_ID;
    Object.assign(container.style, {
      position: "fixed",
      top: "16px",
      right: "16px",
      zIndex: "2147483647",
      display: "flex",
      flexDirection: "column",
      gap: "10px",
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      fontSize: "13px",
    });
    document.body.appendChild(container);
  }
  return container;
}

function showDialog(data) {
  const { index, preview, previewStatus, previewError } = data;
  const container = getOrCreateContainer();

  const card = document.createElement("div");
  card.dataset.hlsIndex = index;
  Object.assign(card.style, {
    background: "#1a1a2e",
    color: "#e0e0e0",
    borderRadius: "10px",
    padding: "14px 16px",
    minWidth: "320px",
    maxWidth: "400px",
    boxShadow: "0 8px 32px rgba(0,0,0,0.6), 0 0 0 1px rgba(124,58,237,0.3)",
    borderLeft: "3px solid #f59e0b",
    animation: "hls-slide-in 0.3s ease-out",
  });

  if (previewStatus === "loading") {
    card.innerHTML = `
      <div style="color:#9ca3af; font-style:italic;">Loading preview...</div>
    `;
    container.appendChild(card);
    return;
  }

  if (previewStatus === "error") {
    card.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
        <span style="font-weight:700; font-size:12px; color:#7c3aed; text-transform:uppercase; letter-spacing:0.5px;">HLS Capture</span>
      </div>
      <div style="color:#fca5a5; margin-bottom:8px;">${previewError || "Preview failed"}</div>
      <div style="display:flex; gap:8px;">
        <button class="hls-dismiss" style="
          background:#374151; color:#9ca3af; border:none; padding:6px 16px;
          border-radius:5px; cursor:pointer; font-size:12px;
        ">Dismiss</button>
      </div>
    `;
  } else {
    const p = preview;
    card.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <span style="font-weight:700; font-size:12px; color:#7c3aed; text-transform:uppercase; letter-spacing:0.5px;">HLS Capture</span>
        <span style="font-size:11px; padding:2px 8px; border-radius:10px; background:#78350f; color:#fbbf24; font-weight:600;">awaiting</span>
      </div>
      <div style="font-weight:600; font-size:15px; color:#f3f4f6; margin-bottom:6px;">
        ${p.show_title}
      </div>
      <div style="display:flex; gap:14px; font-size:12px; color:#9ca3af; margin-bottom:10px;">
        <span style="color:#a78bfa; font-weight:600;">${p.ep_tag}</span>
        <span style="color:#818cf8; font-weight:600;">${p.quality}</span>
      </div>
      <div style="font-size:11px; color:#6b7280; margin-bottom:10px; word-break:break-all;">
        ${p.filename}
      </div>
      <div style="display:flex; gap:8px;">
        <button class="hls-confirm" style="
          background:#065f46; color:#6ee7b7; font-weight:600; border:none;
          padding:7px 20px; border-radius:5px; cursor:pointer; font-size:13px;
        ">Download</button>
        <button class="hls-dismiss" style="
          background:#374151; color:#9ca3af; border:none; padding:7px 16px;
          border-radius:5px; cursor:pointer; font-size:12px;
        ">Dismiss</button>
      </div>
    `;
  }

  // Attach button handlers
  const confirmBtn = card.querySelector(".hls-confirm");
  if (confirmBtn) {
    confirmBtn.addEventListener("click", () => {
      confirmBtn.disabled = true;
      confirmBtn.textContent = "Starting...";
      confirmBtn.style.background = "#374151";
      confirmBtn.style.color = "#6b7280";
      chrome.runtime.sendMessage({ type: "confirmDownload", index }, () => {
        fadeOutCard(card);
      });
    });
    confirmBtn.addEventListener("mouseenter", () => {
      confirmBtn.style.background = "#047857";
    });
    confirmBtn.addEventListener("mouseleave", () => {
      confirmBtn.style.background = "#065f46";
    });
  }

  const dismissBtn = card.querySelector(".hls-dismiss");
  if (dismissBtn) {
    dismissBtn.addEventListener("click", () => {
      chrome.runtime.sendMessage({ type: "dismissCapture", index }, () => {
        fadeOutCard(card);
      });
    });
    dismissBtn.addEventListener("mouseenter", () => {
      dismissBtn.style.background = "#4b5563";
    });
    dismissBtn.addEventListener("mouseleave", () => {
      dismissBtn.style.background = "#374151";
    });
  }

  container.appendChild(card);
}

function fadeOutCard(card) {
  card.style.transition = "opacity 0.3s ease, transform 0.3s ease";
  card.style.opacity = "0";
  card.style.transform = "translateX(60px)";
  setTimeout(() => card.remove(), 300);
}

function updateDialog(data) {
  const container = document.getElementById(DIALOG_ID);
  if (!container) return;

  // Find existing card for this index and replace it
  const existing = container.querySelector(`[data-hls-index="${data.index}"]`);
  if (existing) {
    existing.remove();
  }
  showDialog(data);
}

// Inject slide-in animation
const style = document.createElement("style");
style.textContent = `
  @keyframes hls-slide-in {
    from { opacity: 0; transform: translateX(60px); }
    to { opacity: 1; transform: translateX(0); }
  }
`;
document.head.appendChild(style);

// ========= AUTO-CAPTURE OVERLAY & HELPERS =========

const AC_OVERLAY_ID = "hls-auto-capture-overlay";
let autoCaptureAborted = false;
let autoCaptureResolveEpisode = null;

function showAutoCaptureOverlay(text) {
  let overlay = document.getElementById(AC_OVERLAY_ID);
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = AC_OVERLAY_ID;
    Object.assign(overlay.style, {
      position: "fixed",
      bottom: "16px",
      left: "16px",
      zIndex: "2147483647",
      background: "#1a1a2e",
      color: "#c7d2fe",
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      fontSize: "13px",
      fontWeight: "600",
      padding: "8px 14px",
      borderRadius: "8px",
      borderLeft: "3px solid #6366f1",
      boxShadow: "0 4px 16px rgba(0,0,0,0.5)",
    });
    document.body.appendChild(overlay);
  }
  overlay.textContent = text;
}

function removeAutoCaptureOverlay() {
  const overlay = document.getElementById(AC_OVERLAY_ID);
  if (overlay) overlay.remove();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function sendMessageAsync(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, resolve);
  });
}

function waitForEpisodeDone(timeoutMs) {
  return new Promise((resolve) => {
    autoCaptureResolveEpisode = resolve;
    setTimeout(() => resolve(false), timeoutMs);
  });
}

// ========= SITE DETECTION & DOM HELPERS =========

function getCurrentSite() {
  const host = window.location.hostname;
  if (host.includes("brocoflix.xyz")) return "brocoflix.xyz";
  if (host.includes("1movies.bz")) return "1movies.bz";
  return null;
}

// Find the main embed iframe on the page
function getEmbedIframe() {
  return document.querySelector("#video-player iframe")
    ?? document.querySelector("iframe[src*='vidsrc']")
    ?? document.querySelector("iframe[src*='embed']")
    ?? document.querySelector("iframe");
}

// Force the embed iframe to start playing by setting autoPlay=true in its src.
// This bypasses isTrusted checks on the site's play button handler.
function triggerIframeAutoPlay() {
  const iframe = getEmbedIframe();
  if (!iframe?.src) return false;
  const src = iframe.src;
  if (src.toLowerCase().includes("autoplay=true")) return true; // already set
  if (src.toLowerCase().includes("autoplay=false")) {
    iframe.src = src.replace(/autoPlay=false/i, "autoPlay=true");
    return true;
  }
  // No autoPlay param present — append it
  iframe.src = src + (src.includes("?") ? "&" : "?") + "autoPlay=true";
  return true;
}

// Wait for a DOM element matching selector to appear (MutationObserver-based)
function waitForElement(selector, timeoutMs = 8000) {
  return new Promise((resolve) => {
    const el = document.querySelector(selector);
    if (el) return resolve(el);

    const observer = new MutationObserver(() => {
      const found = document.querySelector(selector);
      if (found) {
        observer.disconnect();
        resolve(found);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    setTimeout(() => {
      observer.disconnect();
      resolve(null);
    }, timeoutMs);
  });
}

// Extract the show title from the page DOM
function getShowTitleFromDom() {
  const selectors = [
    "#details-container h1",
    "#details-container .title",
    "#details-container [class*='title']",
    "h1",
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el?.textContent?.trim()) return el.textContent.trim();
  }
  // Last resort: strip site name suffix from document title
  return document.title.replace(/\s*[\|–\-].*$/, "").trim();
}

// ========= BROCOFLIX EPISODE TRACKING =========

// Listen for episode card / play button / server button clicks on brocoflix.
function setupBrocoflixListeners() {
  document.addEventListener("click", (e) => {
    // --- Episode play button clicked ---
    // .episode-play-button is inside each .episode-card; clicking it starts playback.
    const playBtn = e.target.closest(".episode-play-button");
    if (playBtn) {
      const card = playBtn.closest(".episode-card");
      const season  = parseInt(card?.dataset.season,  10) || null;
      const episode = parseInt(card?.dataset.episode, 10) || null;
      const showName = getShowTitleFromDom();
      if (season && episode) {
        chrome.runtime.sendMessage({ type: "setEpisodeContext", show_name: showName, season, episode });
      }
      return;
    }

    // --- Episode card clicked (without hitting the play button) ---
    const card = e.target.closest(".episode-card");
    if (card) {
      const season  = parseInt(card.dataset.season,  10) || null;
      const episode = parseInt(card.dataset.episode, 10) || null;
      const showName = getShowTitleFromDom();
      if (season && episode) {
        chrome.runtime.sendMessage({ type: "setEpisodeContext", show_name: showName, season, episode });
      }
      return;
    }

    // --- Server button clicked ---
    // When the user switches servers, clear seenM3u8 so the new server's
    // m3u8 URL (which may share a CDN with the previous server) can be
    // captured fresh.
    const serverBtn = e.target.closest(".server-button");
    if (serverBtn) {
      chrome.runtime.sendMessage({ type: "clearEpisodeState" });
    }
  }, true); // capture phase fires before page handlers
}

// ========= AUTO-CAPTURE: CLICK-CARD STRATEGY (brocoflix) =========

// Navigate between episodes by clicking .episode-card elements in the DOM.
// No page reload needed — the site is a SPA and updates the player in-place.
async function runAutoCaptureClickCard(state) {
  const { season, currentEp, startEp, endEp, multiSeason } = state;
  autoCaptureAborted = false;

  if (multiSeason && endEp) {
    showAutoCaptureOverlay(`Auto-capture: S${season} EP ${currentEp}/${endEp}`);
  } else if (multiSeason) {
    showAutoCaptureOverlay(`Auto-capture: S${season} EP ${currentEp} (detecting...)`);
  } else {
    showAutoCaptureOverlay(`Auto-capture: EP ${currentEp} of ${startEp}–${endEp}`);
  }

  // Ensure the correct season is selected in the dropdown
  const seasonSelect = document.querySelector("#season-select");
  if (seasonSelect && parseInt(seasonSelect.value, 10) !== season) {
    seasonSelect.value = String(season);
    seasonSelect.dispatchEvent(new Event("change", { bubbles: true }));
    await sleep(2000);
    if (autoCaptureAborted) return;
  }

  // Wait for episode cards / play buttons to be present in the DOM
  await waitForElement(".episode-play-button", 8000);
  if (autoCaptureAborted) return;

  // Multi-season: on the first episode of each season, count episode cards and report to background
  if (multiSeason && currentEp === 1) {
    let epCount = document.querySelectorAll(`.episode-card[data-season="${season}"]`).length;
    if (epCount === 0) {
      epCount = document.querySelectorAll(".episode-card").length;
    }
    console.log(`[AC] Season S${season}: detected ${epCount} episodes`);
    await sendMessageAsync({ type: "autoCaptureSeasonDetected", episodeCount: epCount });
    // Update the overlay now that we know the count
    showAutoCaptureOverlay(`Auto-capture: S${season} EP ${currentEp}/${epCount}`);
    // Update local state so subsequent overlay updates use the correct endEp
    state.endEp = epCount;
    if (autoCaptureAborted) return;
  }

  // Find the episode card for the target episode.
  // Primary:  data-season + data-episode attributes on .episode-card
  // Fallback: Nth .episode-card (1-indexed, assuming the list is in episode order)
  let card = document.querySelector(`.episode-card[data-season="${season}"][data-episode="${currentEp}"]`)
          || document.querySelector(`.episode-card[data-episode="${currentEp}"]`);

  if (!card) {
    const allCards = document.querySelectorAll(".episode-card");
    card = allCards[currentEp - 1] || null;
    if (card) {
      const label = multiSeason ? `S${season} EP ${currentEp}` : `EP ${currentEp} of ${startEp}–${endEp}`;
      showAutoCaptureOverlay(`Auto-capture: ${label} (index fallback)`);
    }
  }

  if (card) {
    // Set episode context before clicking so it's in place when the m3u8 fires
    const showName = getShowTitleFromDom();
    chrome.runtime.sendMessage({ type: "setEpisodeContext", show_name: showName, season, episode: currentEp });
    chrome.runtime.sendMessage({ type: "autoCaptureClickedPlay", season, episode: currentEp });

    // Scroll the card into view and click it to navigate to the target episode.
    // Clicking the card (not the play button) loads the iframe with the correct
    // episode at autoPlay=false, which we'll then upgrade to autoPlay=true below.
    card.scrollIntoView({ block: "nearest", behavior: "instant" });
    await sleep(200);
    card.click();
    console.log(`[AC] EP ${currentEp}: card clicked`);
    await sleep(800);
    if (autoCaptureAborted) return;

    // Click the preferred server button (always visible, 1-indexed).
    const serverNum = state.serverNum || 1;
    const allServerBtns = document.querySelectorAll(".server-button");
    const serverBtn = allServerBtns[serverNum - 1];
    if (serverBtn && !serverBtn.classList.contains("active")) {
      serverBtn.click();
      console.log(`[AC] EP ${currentEp}: server button ${serverNum} clicked`);
      await sleep(800); // wait for iframe src to update to the selected server
      if (autoCaptureAborted) return;
    }

    // Clear seen state NOW — the old player has had time to settle, so any stale
    // m3u8 requests were blocked by the still-populated seenM3u8 set.  Clearing
    // here ensures the NEW episode's m3u8 (which fires after triggerIframeAutoPlay)
    // passes the seenM3u8 check.  This is awaited to guarantee the clear is
    // processed before any new m3u8 requests fire.
    await sendMessageAsync({ type: "clearEpisodeState" });
    console.log(`[AC] EP ${currentEp}: state cleared, triggering autoplay`);

    // Trigger playback by directly setting autoPlay=true on the iframe src.
    // Programmatic .click() on .episode-play-button is blocked by isTrusted checks;
    // iframe src manipulation bypasses this entirely.
    const autoplayResult = triggerIframeAutoPlay();
    console.log(`[AC] EP ${currentEp}: triggerIframeAutoPlay returned ${autoplayResult}`);
  } else {
    console.log(`[AC] EP ${currentEp}: card NOT FOUND (${document.querySelectorAll(".episode-card").length} cards visible)`);
    const notFoundLabel = multiSeason ? `S${season} EP ${currentEp}` : `EP ${currentEp}`;
    showAutoCaptureOverlay(`Auto-capture: ${notFoundLabel} – card not found (${document.querySelectorAll(".episode-card").length} cards visible)`);
  }

  // Wait for m3u8 capture confirmation (max 15s)
  console.log(`[AC] EP ${currentEp}: waiting for done-signal (15s timeout)`);
  let captured = await waitForEpisodeDone(15000);
  autoCaptureResolveEpisode = null;
  console.log(`[AC] EP ${currentEp}: waitForEpisodeDone returned ${captured}`);
  if (autoCaptureAborted) return;

  // Retry once if not captured
  if (!captured && card) {
    const retryLabel = multiSeason ? `S${season} EP ${currentEp}` : `EP ${currentEp} of ${startEp}–${endEp}`;
    showAutoCaptureOverlay(`Auto-capture: ${retryLabel} (retrying...)`);
    console.log(`[AC] EP ${currentEp}: retrying...`);
    // Reset done-signal so the retry's m3u8 can trigger a fresh one
    await sendMessageAsync({ type: "resetDoneSignal" });
    await sendMessageAsync({ type: "clearEpisodeState" });
    card.click();
    await sleep(800);
    triggerIframeAutoPlay();
    captured = await waitForEpisodeDone(15000);
    autoCaptureResolveEpisode = null;
    console.log(`[AC] EP ${currentEp}: retry waitForEpisodeDone returned ${captured}`);
  }

  if (autoCaptureAborted) return;

  if (!captured) {
    const skipLabel = multiSeason ? `S${season} EP ${currentEp}` : `EP ${currentEp} of ${startEp}–${endEp}`;
    console.log(`[AC] EP ${currentEp}: SKIPPED (no m3u8 captured after 2 attempts)`);
    showAutoCaptureOverlay(`Auto-capture: ${skipLabel} (skipped)`);
    await sleep(1000);
  }

  // Ask background to advance; on success, click the next card directly (no reload)
  chrome.runtime.sendMessage({ type: "autoCaptureAdvance", skipped: !captured }, (resp) => {
    if (chrome.runtime.lastError || !resp) return;

    if (resp.hasNext) {
      runAutoCaptureClickCard({
        season: resp.season,
        currentEp: resp.nextEp,
        startEp: resp.startEp,
        endEp: resp.endEp,
        serverNum: resp.serverNum,
        multiSeason: resp.multiSeason,
      });
    } else {
      showAutoCaptureOverlay("Auto-capture complete!");
      setTimeout(removeAutoCaptureOverlay, 3000);
    }
  });
}

// ========= DOM-BASED EPISODE DISCOVERY (1movies.bz) =========

// Scan the page DOM for episode links matching #ep=season,...
// Returns an ordered array of { hash, epStart, epEnd } objects.
function discoverEpisodeHashes(season) {
  const links = document.querySelectorAll('a[href*="#ep="]');
  const seen = new Set();
  const episodes = [];

  for (const link of links) {
    const href = link.getAttribute("href");
    const m = href && href.match(/#ep=(\d+),(\d+)(?:-(\d+))?/);
    if (!m) continue;
    const linkSeason = parseInt(m[1], 10);
    if (linkSeason !== season) continue;

    const hash = m[0]; // e.g. "#ep=10,8-9"
    if (seen.has(hash)) continue;
    seen.add(hash);

    const epStart = parseInt(m[2], 10);
    const epEnd = m[3] ? parseInt(m[3], 10) : epStart;
    episodes.push({ hash, epStart, epEnd });
  }

  // Sort by episode start number
  episodes.sort((a, b) => a.epStart - b.epStart);
  return episodes;
}

// ========= AUTO-CAPTURE: HASH-RELOAD STRATEGY (1movies.bz) =========

// Navigate between episodes by changing location.hash and reloading the page.
// Required for sites that only load subtitle VTT files on a full page init.
async function runAutoCaptureHashReload(state) {
  const { season, currentEp, endEp, startEp, multiSeason } = state;
  autoCaptureAborted = false;

  // Episode discovery: on first episode of a season (or when background says
  // discovery hasn't happened yet), scan the DOM for episode links and report
  // the list to background.  This gives us exact episode counts and correct
  // hashes for combined episodes (e.g. #ep=10,8-9).
  //
  // Sites like 1movies.bz load episode links via JavaScript AFTER the initial
  // HTML is ready.  At document_idle, only a partial list may be present.
  // We poll until the link count stabilizes to ensure we capture the full list.
  if (state.needsDiscovery) {
    showAutoCaptureOverlay(`Auto-capture: S${season} (discovering episodes...)`);

    let discovered = [];
    let lastCount = 0;
    let stableMs = 0;
    const pollMs = 500;
    const maxWaitMs = 6000;
    const stableThresholdMs = 1500;
    let elapsed = 0;

    while (elapsed < maxWaitMs) {
      discovered = discoverEpisodeHashes(season);
      if (discovered.length > 0 && discovered.length === lastCount) {
        stableMs += pollMs;
        if (stableMs >= stableThresholdMs) break;
      } else {
        lastCount = discovered.length;
        stableMs = 0;
        // Bail early if still 0 links after half the timeout
        if (discovered.length === 0 && elapsed >= maxWaitMs / 2) break;
      }
      await sleep(pollMs);
      elapsed += pollMs;
      if (autoCaptureAborted) return;
    }

    console.log(`[AC] S${season}: discoverEpisodeHashes found ${discovered.length} entries (waited ${elapsed}ms)`);
    if (discovered.length > 0) {
      await sendMessageAsync({
        type: "autoCaptureEpisodesDiscovered",
        episodes: discovered,
        showName: getShowTitleFromDom(),
      });
      // Update local state with the discovery results
      state.endEp = discovered.length;
      state.needsDiscovery = false;
    } else {
      console.log(`[AC] S${season}: discovery found no links — falling back to constructed hashes`);
    }
  }

  // If we have a discovered hash for this episode, ensure location.hash matches it.
  // This handles combined episodes (e.g. #ep=10,8-9) that would be missed by
  // the simple constructed hash (#ep=10,8).
  if (state.hash && location.hash !== state.hash) {
    console.log(`[AC] EP ${currentEp}: re-navigating to discovered hash ${state.hash} (was ${location.hash})`);
    location.hash = state.hash;
    location.reload();
    return;
  }

  // Early redirect detection (safety net — fallback mode only).
  // When discovery is active (state.hash is set), we navigated to the exact
  // discovered hash, so the redirect check is unnecessary.  More importantly,
  // currentEp is the 1-based INDEX into the discovered list, which doesn't
  // match actual episode numbers after combined episodes (e.g. #ep=10,8-9).
  // Only run this check when we don't have a discovered hash.
  if (!state.hash) {
    const hashMatch = location.hash.match(/#ep=(\d+),(\d+)(?:-(\d+))?/);
    if (hashMatch) {
      const actualSeason = parseInt(hashMatch[1], 10);
      const actualEpStart = parseInt(hashMatch[2], 10);
      const actualEpEnd = hashMatch[3] ? parseInt(hashMatch[3], 10) : actualEpStart;

      if (actualSeason !== season || currentEp < actualEpStart || currentEp > actualEpEnd) {
        // True redirect — different season or episode not in range
        console.log(`[AC] EP ${currentEp}: redirect detected! Hash is #ep=${actualSeason},${actualEpStart}${hashMatch[3] ? "-" + actualEpEnd : ""} — skipping`);
        const skipLabel = multiSeason ? `S${season} EP ${currentEp}` : `EP ${currentEp}`;
        showAutoCaptureOverlay(`Auto-capture: ${skipLabel} (redirect — skipped)`);
        await sleep(1000);
        chrome.runtime.sendMessage({ type: "autoCaptureAdvance", skipped: true }, (resp) => {
          if (chrome.runtime.lastError || !resp) return;
          if (resp.hasNext) {
            const nextHash = resp.hash || `#ep=${resp.season},${resp.nextEp}`;
            location.hash = nextHash;
            location.reload();
          } else {
            showAutoCaptureOverlay("Auto-capture complete!");
            setTimeout(removeAutoCaptureOverlay, 3000);
          }
        });
        return;
      }
      // Fall through to normal capture flow
    }
  }

  if (multiSeason) {
    const epLabel = endEp ? `EP ${currentEp}/${endEp}` : `EP ${currentEp}`;
    showAutoCaptureOverlay(`Auto-capture: S${season} ${epLabel}`);
  } else {
    showAutoCaptureOverlay(`Auto-capture: EP ${currentEp} of ${startEp}–${endEp}`);
  }

  // Clear seen state for the new episode — the page has reloaded so old
  // requests are gone, and we need seenM3u8 empty for the new m3u8.
  await sendMessageAsync({ type: "clearEpisodeState" });
  console.log(`[AC] EP ${currentEp}: state cleared (hash-reload)`);

  // Set up the done-listener BEFORE the sleep so we don't miss autoCaptureEpisodeDone
  // messages that arrive during page load (video can auto-play before we click play).
  // Timeout = 4s sleep + 15s capture window.
  const capturePromise = waitForEpisodeDone(19000);

  // Race condition fix: the m3u8 may have fired during page load (before this content
  // script reached document_idle).  autoConfirmCapture would have processed it and set
  // episodeDoneSent=true, but the done-signal was lost because our message listener
  // wasn't registered yet (or autoCaptureResolveEpisode was null).  Check now and
  // resolve immediately if the capture already completed.
  const doneCheck = await sendMessageAsync({ type: "checkEpisodeDone" });
  if (doneCheck?.done && autoCaptureResolveEpisode) {
    console.log(`[AC] EP ${currentEp}: already captured during page load (early resolve)`);
    autoCaptureResolveEpisode(true);
    autoCaptureResolveEpisode = null;
  }

  // Wait for page and player to fully initialize (subtitles load during this time)
  await sleep(4000);
  if (autoCaptureAborted) return;

  // Tell background we're clicking play for this episode
  chrome.runtime.sendMessage({
    type: "autoCaptureClickedPlay",
    season,
    episode: currentEp,
  });

  // Click play
  let playBtn = document.querySelector("#player button.player-btn");
  if (playBtn) playBtn.click();

  // Wait for m3u8 capture confirmation (promise may already be resolved if video auto-played)
  let captured = await capturePromise;
  autoCaptureResolveEpisode = null;

  if (autoCaptureAborted) return;

  // Retry once if not captured
  if (!captured) {
    const retryLabel = multiSeason ? `S${season} EP ${currentEp}` : `EP ${currentEp} of ${startEp}–${endEp}`;
    showAutoCaptureOverlay(`Auto-capture: ${retryLabel} (retrying...)`);
    // Reset done-signal so the retry's m3u8 can trigger a fresh one
    await sendMessageAsync({ type: "resetDoneSignal" });
    await sendMessageAsync({ type: "clearEpisodeState" });
    playBtn = document.querySelector("#player button.player-btn");
    if (playBtn) playBtn.click();

    captured = await waitForEpisodeDone(15000);
    autoCaptureResolveEpisode = null;

    if (!captured && !autoCaptureAborted) {
      showAutoCaptureOverlay(`Auto-capture: ${retryLabel} (skipped)`);
      await sleep(1000);
    }
  }

  if (autoCaptureAborted) return;

  // Ask background to advance; navigate to next episode with a full page reload
  chrome.runtime.sendMessage({ type: "autoCaptureAdvance", skipped: !captured }, (resp) => {
    if (chrome.runtime.lastError || !resp) return;

    if (resp.hasNext) {
      // Use discovered hash when available, fall back to constructed hash
      const nextHash = resp.hash || `#ep=${resp.season},${resp.nextEp}`;
      location.hash = nextHash;
      location.reload();
    } else {
      showAutoCaptureOverlay("Auto-capture complete!");
      setTimeout(removeAutoCaptureOverlay, 3000);
    }
  });
}

// ========= AUTO-CAPTURE DISPATCHER =========

// Dispatches to the correct strategy based on siteConfig.navStrategy.
// Called on every page load (for hash-reload sites) and directly for click-card sites.
function runAutoCaptureStep(state) {
  if (state.siteConfig?.navStrategy === "click-card") {
    runAutoCaptureClickCard(state);
  } else {
    runAutoCaptureHashReload(state);
  }
}

// Called on every page load — if auto-capture is active for this tab, run the current episode step
function checkAutoCaptureOnLoad() {
  chrome.runtime.sendMessage({ type: "checkAutoCapture" }, (state) => {
    if (chrome.runtime.lastError || !state || !state.active) return;
    runAutoCaptureStep(state);
  });
}

// ========= DOM INSPECTOR =========

function inspectPageDom() {
  const lines = [];

  lines.push(`URL:   ${window.location.href}`);
  lines.push(`Title: ${document.title}`);
  lines.push("");

  // #details-container — find the show title
  const dc = document.querySelector("#details-container");
  if (dc) {
    lines.push("#details-container headings/title elements:");
    const titleEls = dc.querySelectorAll("h1, h2, h3, [class*='title'], [class*='name'], [class*='heading']");
    if (titleEls.length) {
      titleEls.forEach((el) => {
        const text = el.textContent.trim().slice(0, 120);
        lines.push(`  <${el.tagName.toLowerCase()} class="${el.className}">`);
        if (text) lines.push(`    "${text}"`);
      });
    } else {
      // Fallback: just show raw text
      const text = dc.textContent.trim().slice(0, 200);
      lines.push(`  (no headings found) text: "${text}"`);
    }
  } else {
    lines.push("#details-container: NOT FOUND");
  }
  lines.push("");

  // #video-player — find any buttons/play elements inside it
  const vp = document.querySelector("#video-player");
  if (vp) {
    lines.push("#video-player contents:");
    const vpEls = vp.querySelectorAll("button, [role='button'], [class*='btn'], [class*='play'], [class*='watch'], [class*='server'], iframe");
    if (vpEls.length) {
      vpEls.forEach((el) => {
        const text = el.tagName === "IFRAME"
          ? `src="${el.src.slice(0, 80)}"`
          : el.textContent.trim().slice(0, 80);
        lines.push(`  <${el.tagName.toLowerCase()} id="${el.id}" class="${el.className}">`);
        if (text) lines.push(`    ${text}`);
      });
    } else {
      lines.push("  (empty or not yet loaded)");
    }
  } else {
    lines.push("#video-player: NOT FOUND");
  }
  lines.push("");

  // All <button> elements on the page
  lines.push("ALL BUTTONS:");
  const buttons = document.querySelectorAll("button");
  if (buttons.length === 0) {
    lines.push("  none");
  } else {
    buttons.forEach((el) => {
      const text = el.textContent.trim().slice(0, 80);
      lines.push(`  id="${el.id}" class="${el.className}"`);
      if (text) lines.push(`    text: "${text}"`);
    });
  }
  lines.push("");

  // Elements with play/watch/server in class or id
  lines.push("PLAY / WATCH / SERVER ELEMENTS:");
  const pwEls = document.querySelectorAll(
    "[class*='play'], [class*='watch'], [class*='server'], [id*='play'], [id*='watch']"
  );
  if (pwEls.length === 0) {
    lines.push("  none found");
  } else {
    pwEls.forEach((el) => {
      const text = el.textContent.trim().slice(0, 80);
      lines.push(`  <${el.tagName.toLowerCase()} id="${el.id}" class="${el.className}">`);
      if (text) lines.push(`    text: "${text}"`);
    });
  }
  lines.push("");

  // Episode cards
  lines.push("EPISODE CARDS (first 3):");
  const cards = document.querySelectorAll(".episode-card");
  if (cards.length === 0) {
    lines.push("  .episode-card: none found");
    // Broader search for anything that looks like an episode list
    const epCandidates = document.querySelectorAll(
      "[class*='episode'], [class*='Episode'], [data-episode], [data-ep], " +
      "[class*='ep-item'], [class*='ep_item'], [class*='epItem']"
    );
    if (epCandidates.length > 0) {
      lines.push(`  Possible episode elements (${epCandidates.length} found):`);
      Array.from(epCandidates).slice(0, 5).forEach((el) => {
        const tag = el.tagName.toLowerCase();
        const attrs = Array.from(el.attributes).map(a => `${a.name}="${a.value}"`).join(" ");
        const text = el.textContent.trim().slice(0, 60);
        lines.push(`    <${tag} ${attrs}>`);
        if (text) lines.push(`      "${text}"`);
      });
    } else {
      lines.push("  No [class*='episode'] or [data-episode] elements found either");
      // Last resort: show clickable list items near the player
      const listItems = document.querySelectorAll("li a, .list-group-item, [role='listitem']");
      if (listItems.length > 0) {
        lines.push(`  Generic list items (${listItems.length} found, first 5):`);
        Array.from(listItems).slice(0, 5).forEach((el) => {
          const tag = el.tagName.toLowerCase();
          const cls = el.className?.toString().slice(0, 80) || "";
          const text = el.textContent.trim().slice(0, 60);
          lines.push(`    <${tag} class="${cls}"> "${text}"`);
        });
      }
    }
  } else {
    Array.from(cards).slice(0, 3).forEach((card) => {
      lines.push(`  data-season="${card.dataset.season}" data-episode="${card.dataset.episode}" class="${card.className}"`);
      const subtitle = card.querySelector("[class*='title'], [class*='name'], p, span")?.textContent.trim().slice(0, 60);
      if (subtitle) lines.push(`    "${subtitle}"`);
    });
    lines.push(`  (${cards.length} total cards)`);
  }
  lines.push("");

  // Season select
  const ss = document.querySelector("#season-select");
  if (ss) {
    const opts = Array.from(ss.options).map((o) => o.value).join(", ");
    lines.push(`#season-select: value="${ss.value}"  options=[${opts}]`);
  } else {
    lines.push("#season-select: NOT FOUND");
  }
  lines.push("");

  // Episode hash links (1movies.bz style: a[href*="#ep="])
  const epLinks = document.querySelectorAll('a[href*="#ep="]');
  lines.push(`EPISODE HASH LINKS (a[href*="#ep="]): ${epLinks.length} total`);
  if (epLinks.length > 0) {
    Array.from(epLinks).slice(0, 5).forEach((link) => {
      const href = link.getAttribute("href");
      const text = link.textContent.trim().slice(0, 60);
      lines.push(`  href="${href}"  text="${text}"`);
    });
    if (epLinks.length > 5) {
      lines.push(`  ... and ${epLinks.length - 5} more`);
    }
  }

  return lines.join("\n");
}

// ========= INIT =========

// Set up site-specific DOM listeners (top frame only — iframes have a
// different origin and no episode cards)
if (IS_TOP_FRAME && getCurrentSite() === "brocoflix.xyz") {
  setupBrocoflixListeners();

  // Diagnostic: log BrocoFlix DOM state to service worker console
  setTimeout(() => {
    const cards = document.querySelectorAll(".episode-card");
    const playBtns = document.querySelectorAll(".episode-play-button");
    const serverBtns = document.querySelectorAll(".server-button");
    const seasonSel = document.querySelector("#season-select");
    const showTitle = getShowTitleFromDom();
    // Send a dedicated diagnostic message (not setEpisodeContext — that pollutes state)
    chrome.runtime.sendMessage({
      type: "brocoflixDiag",
      info: `title="${showTitle}" cards=${cards.length} playBtns=${playBtns.length} serverBtns=${serverBtns.length} seasonSelect=${!!seasonSel}`,
    });
  }, 3000);
}

// ========= BROCOFLIX CHUNK RELAY =========
// Relay MAIN-world download chunks to background service worker, which
// forwards them to the local server.  This relay is needed because:
// 1. MAIN-world can't POST to localhost (mixed content / CSP)
// 2. MV3 content scripts share the page's network context, not the extension's
// So we relay through chrome.runtime.sendMessage → background → localhost.

window.addEventListener("message", (e) => {
  const msg = e.data;
  if (!msg?.type?.startsWith("hlsBroco")) return;

  if (msg.type === "hlsBrocoLog") {
    // Relay MAIN-world log to service worker console (iframe console is inaccessible)
    chrome.runtime.sendMessage({ type: "brocoflixLog", msg: msg.msg });
  } else if (msg.type === "hlsBrocoStart") {
    console.log(`[BF-relay] Download starting: ${msg.totalChunks} segments`);
  } else if (msg.type === "hlsBrocoChunk") {
    // Decode base64 to binary and POST to server via background
    const { sessionId, chunkIndex, totalChunks, data } = msg;
    // Send chunk to background which POSTs to localhost
    chrome.runtime.sendMessage({
      type: "brocoflixChunkData",
      sessionId,
      chunkIndex,
      totalChunks,
      data,  // base64 string
    }, (resp) => {
      if (chrome.runtime.lastError) {
        console.log(`[BF-relay] chunk ${chunkIndex} send failed: ${chrome.runtime.lastError.message}`);
      }
    });
  } else if (msg.type === "hlsBrocoDone") {
    console.log(`[BF-relay] All segments downloaded, signaling done`);
    chrome.runtime.sendMessage({
      type: "brocoflixDoneSignal",
      sessionId: msg.sessionId,
    });
  } else if (msg.type === "hlsBrocoNeedReload") {
    console.log(`[BF-relay] Reload requested (${msg.reason}): ${msg.completedIndices.length}/${msg.totalSegments} done`);
    chrome.runtime.sendMessage({
      type: "brocoflixNeedReload",
      sessionId: msg.sessionId,
      completedIndices: msg.completedIndices,
      totalSegments: msg.totalSegments,
      reason: msg.reason,
    });
  } else if (msg.type === "hlsBrocoError") {
    console.log(`[BF-relay] Download error: ${msg.error}`);
    chrome.runtime.sendMessage({
      type: "brocoflixErrorSignal",
      sessionId: msg.sessionId,
      error: msg.error,
    });
  }
});

// On content script load, check if auto-capture is in progress for this tab
if (IS_TOP_FRAME) {
  checkAutoCaptureOnLoad();
}

// ========= MESSAGE LISTENER =========

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!IS_TOP_FRAME) return;
  if (msg.type === "showCaptureDialog") {
    showDialog(msg);
    sendResponse({ ok: true });
  } else if (msg.type === "updateCaptureDialog") {
    updateDialog(msg);
    sendResponse({ ok: true });
  } else if (msg.type === "beginAutoCapture") {
    if (msg.siteConfig?.navStrategy === "click-card") {
      // SPA site — no reload needed, run the loop directly
      runAutoCaptureClickCard({
        season: msg.season,
        currentEp: msg.startEp,
        startEp: msg.startEp,
        endEp: msg.endEp,
        serverNum: msg.serverNum,
        multiSeason: msg.multiSeason,
      });
    } else {
      // Hash-reload: navigate to first episode via hash change + reload
      location.hash = `#ep=${msg.season},${msg.startEp}`;
      location.reload();
    }
    sendResponse({ ok: true });
  } else if (msg.type === "stopAutoCapture") {
    autoCaptureAborted = true;
    if (autoCaptureResolveEpisode) {
      autoCaptureResolveEpisode(false);
      autoCaptureResolveEpisode = null;
    }
    removeAutoCaptureOverlay();
    sendResponse({ ok: true });
  } else if (msg.type === "autoCaptureEpisodeDone") {
    console.log(`[AC] received autoCaptureEpisodeDone for ep ${msg.episode}, resolver=${autoCaptureResolveEpisode ? 'active' : 'null'}`);
    if (autoCaptureResolveEpisode) {
      autoCaptureResolveEpisode(true);
      autoCaptureResolveEpisode = null;
    }
    sendResponse({ ok: true });
  } else if (msg.type === "inspectDom") {
    sendResponse({ result: inspectPageDom() });
  }
  return true;
});
