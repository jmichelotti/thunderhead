const SERVER_BASE = "http://localhost:9876";

async function checkServer() {
  const el = document.getElementById("serverStatus");
  try {
    const resp = await fetch(`${SERVER_BASE}/status`, { signal: AbortSignal.timeout(2000) });
    if (resp.ok) {
      const data = await resp.json();
      const mode = data.dry_run ? "dry run" : "live";
      el.textContent = `server up (${mode})`;
      el.className = "server-status server-up";
    } else {
      throw new Error();
    }
  } catch {
    el.textContent = "server down";
    el.className = "server-status server-down";
  }
}

function renderPending(pendingCaptures) {
  const container = document.getElementById("pending");

  if (!pendingCaptures || pendingCaptures.length === 0) {
    container.innerHTML = "";
    return;
  }

  container.innerHTML =
    '<div class="section-label">Awaiting Confirmation</div>' +
    pendingCaptures.map((p, idx) => {
      if (p.previewStatus === "loading") {
        return `
          <div class="pending-card loading">
            <div class="pending-loading">Loading preview...</div>
          </div>
        `;
      }

      if (p.previewStatus === "error") {
        return `
          <div class="pending-card error">
            <div class="pending-error">${p.previewError || "Preview failed"}</div>
            <div class="pending-actions">
              <button class="btn-dismiss" data-dismiss="${idx}">Dismiss</button>
            </div>
          </div>
        `;
      }

      const preview = p.preview;
      return `
        <div class="pending-card">
          <div class="pending-title">${preview.filename}</div>
          <div class="pending-meta">
            <span class="episode">${preview.show_title}</span>
            <span class="episode">${preview.ep_tag}</span>
            <span class="quality">${preview.quality}</span>
          </div>
          <div class="pending-actions">
            <button class="btn-download" data-confirm="${idx}">Download</button>
            <button class="btn-dismiss" data-dismiss="${idx}">Dismiss</button>
          </div>
        </div>
      `;
    }).join("");

  // Attach event listeners
  container.querySelectorAll("[data-confirm]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.confirm);
      btn.disabled = true;
      btn.textContent = "Starting...";
      chrome.runtime.sendMessage({ type: "confirmDownload", index: idx }, () => {
        refresh();
      });
    });
  });

  container.querySelectorAll("[data-dismiss]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.dismiss);
      chrome.runtime.sendMessage({ type: "dismissCapture", index: idx }, () => {
        refresh();
      });
    });
  });
}

function renderDownloads(downloads) {
  const container = document.getElementById("downloads");

  if (!downloads || downloads.length === 0) {
    container.innerHTML = '<div class="empty">No downloads yet. Browse to a video page and press play.</div>';
    return;
  }

  // Show most recent first
  const sorted = [...downloads].reverse();
  container.innerHTML =
    '<div class="section-label">Downloads</div>' +
    sorted.map((dl) => {
      const pct = dl.percent ? dl.percent.toFixed(1) : "0.0";
      const isDone = dl.status === "done";
      const isActive = dl.status === "downloading";

      // Meta info line
      const metaParts = [];
      if (dl.quality) metaParts.push(`<span class="quality">${dl.quality} kbps</span>`);
      if (dl.size) metaParts.push(`<span>~${dl.size}</span>`);
      if (dl.speed && isActive) metaParts.push(`<span>${dl.speed}</span>`);
      if (dl.eta && isActive) metaParts.push(`<span>ETA ${dl.eta}</span>`);
      if (dl.frag && dl.total_frags) metaParts.push(`<span>frag ${dl.frag}/${dl.total_frags}</span>`);

      const statusLabel = {
        queued: "queued",
        downloading: "downloading",
        uploading: "uploading",
        muxing: "muxing",
        moving: "moving file",
        done: "complete",
        error: "failed",
        dry_run: "dry run",
      }[dl.status] || dl.status;

      return `
        <div class="dl-card ${dl.status}">
          <div class="dl-header">
            <span class="dl-filename">${dl.filename}</span>
            <span class="dl-status ${dl.status}">${statusLabel}</span>
          </div>
          ${metaParts.length ? `<div class="dl-meta">${metaParts.join("")}</div>` : ""}
          <div class="progress-bar-bg">
            <div class="progress-bar-fill ${isDone ? "done" : ""}" style="width: ${pct}%"></div>
          </div>
          <div class="progress-text">
            <span>${pct}%</span>
            <span>${dl.show || ""}</span>
          </div>
        </div>
      `;
    }).join("");
}

async function loadDownloads() {
  try {
    const resp = await fetch(`${SERVER_BASE}/downloads`, { signal: AbortSignal.timeout(2000) });
    if (resp.ok) {
      const data = await resp.json();
      renderDownloads(data.downloads);
    }
  } catch {
    // Server down, keep current display
  }
}

function loadPending() {
  chrome.runtime.sendMessage({ type: "getCaptures" }, (response) => {
    if (response) {
      renderPending(response.pendingCaptures);
    }
  });
}

function refresh() {
  checkServer();
  loadPending();
  loadDownloads();
}

document.getElementById("clearBtn").addEventListener("click", () => {
  // Clear both extension state and server download history
  chrome.runtime.sendMessage({ type: "clearCaptures" }, () => {
    refresh();
  });
  fetch(`${SERVER_BASE}/clear`, { signal: AbortSignal.timeout(2000) }).catch(() => {});
});

// --- Auto-capture controls ---

const acSection = document.getElementById("autoCapture");
const acSite = document.getElementById("acSite");
const acSeasonInput = document.getElementById("acSeasonInput");
const acFromEp = document.getElementById("acFromEp");
const acToEp = document.getElementById("acToEp");
const acServerInput = document.getElementById("acServerInput");
const acStartBtn = document.getElementById("acStartBtn");
const acStopBtn = document.getElementById("acStopBtn");
const acIdle = document.getElementById("acIdle");
const acRunning = document.getElementById("acRunning");
const acIdleStatus = document.getElementById("acIdleStatus");
const acRunStatus = document.getElementById("acRunStatus");
const acModeTabs = document.getElementById("acModeTabs");
const acSeasonEndWrap = document.getElementById("acSeasonEndWrap");
const acSeasonEndInput = document.getElementById("acSeasonEndInput");
const acEpWrap = document.getElementById("acEpWrap");

const SUPPORTED_SITES = ["1movies", "brocoflix.xyz"];

// Mode: "episodes" (single season, ep range) or "seasons" (multi-season, auto-detect ep count)
let acMode = "episodes";

acModeTabs.addEventListener("click", (e) => {
  const tab = e.target.closest(".ac-mode-tab");
  if (!tab || tab.dataset.mode === acMode) return;
  acMode = tab.dataset.mode;
  acModeTabs.querySelectorAll(".ac-mode-tab").forEach(t => t.classList.remove("active"));
  tab.classList.add("active");
  acEpWrap.style.display = acMode === "episodes" ? "" : "none";
  acSeasonEndWrap.style.display = acMode === "seasons" ? "" : "none";
});

// Prevent auto-fill from clobbering user-edited season input
let acSeasonDirty = false;
acSeasonInput.addEventListener("input", () => { acSeasonDirty = true; });
acSeasonEndInput.addEventListener("input", () => { acSeasonDirty = true; });

function getSiteFromUrl(url) {
  if (!url) return null;
  for (const site of SUPPORTED_SITES) {
    if (url.includes(site)) return site;
  }
  return null;
}

// Parse season from 1movies.bz URL hash (#ep=season,episode)
function parseSeasonFromUrl(url) {
  if (!url) return null;
  const match = url.match(/#ep=(\d+),(\d+)/);
  return match ? parseInt(match[1], 10) : null;
}

function updateAutoCaptureUI() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs[0];
    if (!tab) return;

    const site = getSiteFromUrl(tab.url);
    const serverUp = document.getElementById("serverStatus").classList.contains("server-up");

    if (site && serverUp) {
      acSection.classList.remove("hidden");
      acSite.textContent = site;

      // Auto-fill season from URL hash on 1movies.bz (only if user hasn't manually edited it)
      if (site === "1movies" && !acSeasonDirty) {
        const season = parseSeasonFromUrl(tab.url);
        if (season !== null) acSeasonInput.value = season;
      }
    } else {
      acSection.classList.add("hidden");
    }
  });

  // Sync running state from background
  chrome.runtime.sendMessage({ type: "getAutoCaptureState" }, (state) => {
    if (!state) return;

    if (state.active) {
      acSection.classList.remove("hidden");
      acSection.classList.add("active");
      acIdle.style.display = "none";
      acRunning.style.display = "";
      const current = state.currentEp || state.startEp;
      if (state.multiSeason) {
        const seasonLabel = `S${state.currentSeason}`;
        const epRange = state.endEp ? `EP ${current}/${state.endEp}` : `EP ${current}`;
        const progress = state.totalCount > 0 ? `${state.doneCount}/${state.totalCount}` : `${state.doneCount}`;
        acRunStatus.textContent = `${seasonLabel} ${epRange} (${progress} captured)`;
      } else {
        acRunStatus.textContent = `EP ${current} of ${state.startEp}–${state.endEp}...`;
        if (state.doneCount !== undefined) {
          acRunStatus.textContent = `EP ${current} of ${state.startEp}–${state.endEp} (${state.doneCount} captured)`;
        }
      }
    } else if (state.finished) {
      acSection.classList.add("active");
      acIdle.style.display = "";
      acRunning.style.display = "none";
      acIdleStatus.textContent = `Done (${state.doneCount}/${state.totalCount} captured)`;
      acIdleStatus.className = "ac-status done";
    } else {
      acSection.classList.remove("active");
      acIdle.style.display = "";
      acRunning.style.display = "none";
    }
  });
}

acStartBtn.addEventListener("click", () => {
  const season = parseInt(acSeasonInput.value, 10);

  if (!season || season < 1) {
    acIdleStatus.textContent = "Enter a season number";
    acIdleStatus.className = "ac-status error";
    return;
  }

  const serverNum = parseInt(acServerInput.value, 10) || 1;

  if (acMode === "seasons") {
    // Multi-season mode
    const endSeason = parseInt(acSeasonEndInput.value, 10);
    if (!endSeason || season > endSeason) {
      acIdleStatus.textContent = "Invalid season range";
      acIdleStatus.className = "ac-status error";
      return;
    }

    acStartBtn.disabled = true;
    acIdleStatus.textContent = "Starting...";

    chrome.runtime.sendMessage({
      type: "startAutoCapture",
      multiSeason: true,
      startSeason: season,
      endSeason,
      serverNum,
    }, (resp) => {
      acStartBtn.disabled = false;
      if (resp && resp.ok) {
        acIdle.style.display = "none";
        acRunning.style.display = "";
        acRunStatus.textContent = `Capturing S${season}–S${endSeason}...`;
      } else {
        acIdleStatus.textContent = resp?.error || "Failed to start";
        acIdleStatus.className = "ac-status error";
      }
    });
  } else {
    // Episode range mode (existing behavior)
    const startEp = parseInt(acFromEp.value, 10);
    const endEp = parseInt(acToEp.value, 10);

    if (!startEp || !endEp || startEp > endEp || startEp < 1) {
      acIdleStatus.textContent = "Invalid episode range";
      acIdleStatus.className = "ac-status error";
      return;
    }

    acStartBtn.disabled = true;
    acIdleStatus.textContent = "Starting...";

    chrome.runtime.sendMessage({
      type: "startAutoCapture",
      season,
      startEp,
      endEp,
      serverNum,
    }, (resp) => {
      acStartBtn.disabled = false;
      if (resp && resp.ok) {
        acIdle.style.display = "none";
        acRunning.style.display = "";
        acRunStatus.textContent = `Capturing EP ${startEp} of ${startEp}–${endEp}...`;
      } else {
        acIdleStatus.textContent = resp?.error || "Failed to start";
        acIdleStatus.className = "ac-status error";
      }
    });
  }
});

// --- DOM Inspector ---

const inspectBtn = document.getElementById("inspectBtn");
const copyInspectBtn = document.getElementById("copyInspectBtn");
const inspectResults = document.getElementById("inspectResults");
const inspectStatus = document.getElementById("inspectStatus");

inspectBtn.addEventListener("click", () => {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs[0];
    if (!tab) return;

    inspectStatus.textContent = "inspecting...";
    inspectResults.style.display = "none";
    copyInspectBtn.style.display = "none";

    chrome.tabs.sendMessage(tab.id, { type: "inspectDom" }, (resp) => {
      if (chrome.runtime.lastError || !resp) {
        inspectStatus.textContent = "error — are you on a streaming page?";
        return;
      }
      inspectStatus.textContent = "";
      inspectResults.textContent = resp.result;
      inspectResults.style.display = "block";
      copyInspectBtn.style.display = "";
    });
  });
});

copyInspectBtn.addEventListener("click", () => {
  navigator.clipboard.writeText(inspectResults.textContent).then(() => {
    copyInspectBtn.textContent = "Copied!";
    setTimeout(() => { copyInspectBtn.textContent = "Copy"; }, 1500);
  });
});

acStopBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "stopAutoCapture" }, () => {
    acIdle.style.display = "";
    acRunning.style.display = "none";
    acIdleStatus.textContent = "Stopped";
    acIdleStatus.className = "ac-status error";
    acSection.classList.remove("active");
  });
});

refresh();

// Refresh every 1 second while popup is open
setInterval(() => {
  refresh();
  updateAutoCaptureUI();
}, 1000);

// Initial auto-capture UI check
updateAutoCaptureUI();
