const API = window.location.origin;
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

async function api(path) {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

async function tryApi(path, fallback) {
  try { return await api(path); } catch { return fallback; }
}

// ── Nav ─────────────────────────────────────────────

const views = {
  wrapped: renderWrapped,
  "now-watching": renderNowWatching,
  library: renderLibrary,
  missing: renderMissing,
};

$$(".nav-btn").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$(".nav-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    loadView(btn.dataset.view);
  })
);

async function loadView(name) {
  $("#app").innerHTML = '<div class="loading-wrap"><div class="loading-spinner"></div></div>';
  try {
    await views[name]();
    staggerAnimations();
  } catch (e) {
    $("#app").innerHTML = `<div class="error-msg">${esc(e.message)}</div>`;
  }
}

function staggerAnimations() {
  $$(".anim").forEach((el, i) => {
    el.style.animationDelay = `${i * 0.06}s`;
  });
}

// ── Wrapped ─────────────────────────────────────────

async function renderWrapped() {
  const [wrapped, sessions, heatmap] = await Promise.all([
    api("/playback/wrapped"),
    tryApi("/sessions", { active_streams: 0, streams: [] }),
    api("/playback/hourly"),
  ]);

  let html = "";

  if (sessions.active_streams > 0) {
    html += nowPlayingBanner(sessions);
  }

  const range = wrapped.data_range || {};
  const since = range.earliest ? `Since ${fmtDate(range.earliest)}` : "";

  html += `
    <div class="stats-row anim">
      <div class="stat-card">
        <div class="label">Total Watch Time</div>
        <div class="value">${wrapped.totals.time_human}</div>
        <div class="sub">${since}</div>
      </div>
      <div class="stat-card">
        <div class="label">Total Plays</div>
        <div class="value">${num(wrapped.totals.plays)}</div>
        <div class="sub">${wrapped.users.length} active users</div>
      </div>
      <div class="stat-card">
        <div class="label">Avg Per User</div>
        <div class="value">${humanTime(Math.round(wrapped.totals.time_s / (wrapped.users.length || 1)))}</div>
        <div class="sub">watch time</div>
      </div>
    </div>`;

  html += '<div class="section anim"><div class="section-title">Per-User Wrapped</div>';
  html += '<div class="section-sub">Top shows and movies by watch time</div>';
  html += '<div class="user-grid">';
  wrapped.users.forEach((u, i) => (html += userCard(u, i)));
  html += "</div></div>";

  html += '<div class="section anim"><div class="section-title">Viewing Heatmap</div>';
  html += '<div class="section-sub">Plays by day of week and hour (UTC)</div>';
  html += heatmapBlock(heatmap.heatmap);
  html += "</div>";

  $("#app").innerHTML = html;
}

function userCard(u, idx) {
  const delay = idx * 0.05;

  let shows = "";
  if (u.top_shows.length) {
    shows = `<div class="top-label">Top Shows</div><ul class="top-list">`;
    u.top_shows.forEach(
      (s, i) =>
        (shows += `<li><span class="rank">${i + 1}</span><span class="title">${esc(s.name)}</span><span class="time">${s.time_human}</span></li>`)
    );
    shows += "</ul>";
  }

  let movies = "";
  if (u.top_movies.length) {
    movies = `<div class="top-label">Top Movies</div><ul class="top-list">`;
    u.top_movies.forEach(
      (m, i) =>
        (movies += `<li><span class="rank">${i + 1}</span><span class="title">${esc(m.name)}</span><span class="time">${m.time_human}</span></li>`)
    );
    movies += "</ul>";
  }

  return `
    <div class="user-card anim" style="animation-delay:${delay}s">
      <div class="user-header">
        <span class="username">${esc(u.user)}</span>
        <span class="user-time">${u.total_time_human}</span>
      </div>
      <div class="user-plays">${num(u.total_plays)} plays</div>
      ${shows}${movies}
    </div>`;
}

function heatmapBlock(data) {
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  let max = 0;
  for (const d of days) {
    if (!data[d]) continue;
    for (const v of Object.values(data[d])) if (v > max) max = v;
  }

  let g = '<div class="heatmap-wrap"><div class="heatmap-grid">';
  g += '<div class="heatmap-label"></div>';
  for (let h = 0; h < 24; h++) {
    const label = h % 3 === 0 ? h : "";
    g += `<div class="heatmap-hour">${label}</div>`;
  }

  for (const day of days) {
    g += `<div class="heatmap-label">${day}</div>`;
    for (let h = 0; h < 24; h++) {
      const key = `${String(h).padStart(2, "0")}:00`;
      const val = (data[day] && data[day][key]) || 0;
      const lv = max > 0 ? Math.min(Math.ceil((val / max) * 5), 5) : 0;
      g += `<div class="heatmap-cell heat-${lv}" title="${day} ${key}: ${val} plays"></div>`;
    }
  }
  g += "</div></div>";
  return g;
}

// ── Now Watching ────────────────────────────────────

async function renderNowWatching() {
  const [sessions, watching] = await Promise.all([
    tryApi("/sessions", { active_streams: 0, streams: [] }),
    api("/playback/currently-watching"),
  ]);

  let html = "";

  if (sessions.active_streams > 0) {
    html += nowPlayingBanner(sessions);
  }

  html += '<div class="section anim"><div class="section-title">Currently Watching</div>';
  html += '<div class="section-sub">Shows with activity in the last 30 days</div>';

  for (const u of watching.users) {
    html += `<div class="watching-user anim">`;
    html += `<div class="watching-username">${esc(u.user)}</div>`;
    for (const s of u.shows) {
      html += `
        <div class="show-row">
          <div>
            <div class="show-name">${esc(s.show)}</div>
            <div class="show-meta">Last: ${esc(s.last_episode)} &middot; ${fmtDate(s.last_watched)}</div>
          </div>
          <div class="show-right">
            <div class="show-eps">${s.episodes_watched} ep${s.episodes_watched !== 1 ? "s" : ""}</div>
            <div class="show-time">${s.total_time_human}</div>
          </div>
        </div>`;
    }
    html += "</div>";
  }

  html += "</div>";
  $("#app").innerHTML = html;
}

// ── Library ─────────────────────────────────────────

async function renderLibrary() {
  const [lib, sessions, users] = await Promise.all([
    api("/library"),
    tryApi("/sessions", { active_streams: 0, streams: [] }),
    api("/users"),
  ]);

  let html = "";

  if (sessions.active_streams > 0) {
    html += nowPlayingBanner(sessions);
  }

  html += `
    <div class="stats-row anim">
      <div class="stat-card">
        <div class="label">Movies</div>
        <div class="value">${num(lib.movies)}</div>
      </div>
      <div class="stat-card">
        <div class="label">TV Series</div>
        <div class="value">${num(lib.series)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Episodes</div>
        <div class="value">${num(lib.episodes)}</div>
      </div>
    </div>`;

  if (lib.storage && lib.storage.length) {
    html += '<div class="section anim"><div class="section-title">Storage</div>';
    html += '<div class="section-sub">Disk usage across library drives</div>';
    for (const d of lib.storage) {
      if (d.drive === "TOTAL") continue;
      const pct = d.total_gb > 0 ? ((d.used_gb / d.total_gb) * 100).toFixed(1) : 0;
      const critical = pct > 90 ? " critical" : "";
      html += `
        <div class="storage-row">
          <div class="storage-label">
            <strong>${esc(d.drive)}</strong>
            <span>${num(d.used_gb)} / ${num(d.total_gb)} GB &middot; ${pct}%</span>
          </div>
          <div class="storage-track">
            <div class="storage-fill${critical}" style="width:${pct}%"></div>
          </div>
        </div>`;
    }
    const total = lib.storage.find((d) => d.drive === "TOTAL");
    if (total) {
      html += `<div class="storage-total">${num(total.used_gb)} / ${num(total.total_gb)} GB total &middot; ${num(total.free_gb)} GB free</div>`;
    }
    html += "</div>";
  }

  html += '<div class="section anim"><div class="section-title">Users</div>';
  html += '<div class="table-wrap"><table>';
  html += "<tr><th>User</th><th>Last Active</th><th>Last Login</th><th>Role</th></tr>";

  const sorted = [...users].sort((a, b) => (b.last_activity || "").localeCompare(a.last_activity || ""));
  for (const u of sorted) {
    const role = u.is_admin
      ? '<span class="table-role admin">admin</span>'
      : '<span class="table-role user">user</span>';
    const active = u.last_activity
      ? fmtDate(u.last_activity)
      : '<span class="table-dim">never</span>';
    const login = u.last_login
      ? fmtDate(u.last_login)
      : '<span class="table-dim">never</span>';

    html += `<tr><td><strong>${esc(u.name)}</strong></td><td>${active}</td><td>${login}</td><td>${role}</td></tr>`;
  }

  html += "</table></div></div>";
  $("#app").innerHTML = html;
}

// ── Missing Episodes ───────────────────────────────

async function renderMissing() {
  const gaps = await api("/episodes/gaps");

  let html = "";

  html += `
    <div class="stats-row anim">
      <div class="stat-card">
        <div class="label">Shows With Gaps</div>
        <div class="value">${gaps.shows_with_gaps}</div>
      </div>
      <div class="stat-card">
        <div class="label">Missing Episodes</div>
        <div class="value">${num(gaps.total_missing_episodes)}</div>
      </div>
      <div class="stat-card">
        <div class="label">Last Scanned</div>
        <div class="value" style="font-size:1.2rem">${fmtDate(gaps.generated_at)}</div>
        <div class="sub">${new Date(gaps.generated_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}</div>
      </div>
    </div>`;

  if (gaps.shows_with_gaps === 0) {
    html += `
      <div class="missing-clear anim">
        <div class="missing-clear-title">All caught up</div>
        <div class="missing-clear-sub">No missing episodes detected for any currently-watched shows.</div>
      </div>`;
    $("#app").innerHTML = html;
    return;
  }

  html += '<div class="section anim"><div class="section-title">Episodes to Download</div>';
  html += '<div class="section-sub">Aired episodes missing from Jellyfin for shows users are watching</div>';

  for (const show of gaps.gaps) {
    html += `<div class="missing-show anim">`;
    html += `<div class="missing-show-header">`;
    html += `<div>`;
    html += `<div class="missing-show-name">${esc(show.show)}</div>`;
    html += `<div class="missing-show-meta">${num(show.total_in_jellyfin)} episodes in library &middot; ${show.missing_episodes.length} missing</div>`;
    html += `</div>`;
    html += `<div class="missing-count">${show.missing_episodes.length}</div>`;
    html += `</div>`;

    html += `<div class="missing-episodes">`;
    for (const ep of show.missing_episodes) {
      html += `
        <div class="missing-ep-row">
          <span class="missing-ep-code">${esc(ep.code)}</span>
          <span class="missing-ep-title">${esc(ep.title)}</span>
          <span class="missing-ep-date">${fmtDate(ep.airdate)}</span>
        </div>`;
    }
    html += `</div></div>`;
  }

  html += "</div>";
  $("#app").innerHTML = html;
}

// ── Now Playing Banner ──────────────────────────────

function nowPlayingBanner(sessions) {
  let streams = "";
  for (const s of sessions.streams) {
    const np = s.now_playing;
    let title;
    if (np.series_name) {
      title = `<em>${esc(np.series_name)}</em> &middot; S${np.season ?? "?"}E${np.episode ?? "?"} &middot; ${esc(np.name)}`;
    } else {
      title = `<em>${esc(np.name)}</em>${np.year ? ` (${np.year})` : ""}`;
    }

    const pct = s.progress_pct || 0;
    const method = s.play_method !== "DirectPlay" ? `<span class="np-method">${esc(s.play_method)}</span>` : "";

    streams += `
      <div class="np-stream">
        <span class="np-user">${esc(s.user)}</span>
        <span class="np-title">${title}</span>
        <div class="np-right">
          ${Math.round(pct)}%
          <span class="progress-track"><span class="progress-fill" style="width:${pct}%"></span></span>
          ${method}
        </div>
      </div>`;
  }

  const n = sessions.active_streams;
  return `
    <div class="now-playing anim">
      <div class="np-label">Now Playing &middot; ${n} stream${n !== 1 ? "s" : ""}</div>
      ${streams}
    </div>`;
}

// ── Helpers ─────────────────────────────────────────

function esc(s) {
  if (!s) return "";
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function num(n) {
  return n != null ? n.toLocaleString() : "0";
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function humanTime(sec) {
  if (!sec || sec <= 0) return "0m";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const p = [];
  if (d) p.push(`${d}d`);
  if (h) p.push(`${h}h`);
  p.push(`${m}m`);
  return p.join(" ");
}

// ── Init ────────────────────────────────────────────

async function init() {
  try {
    const status = await api("/status");
    const badge = $("#server-badge");
    if (status.server.online) {
      badge.querySelector(".badge-text").textContent =
        `${status.server.server_name} v${status.server.version}`;
      badge.className = "badge online";
    }
  } catch {
    // stays offline
  }
  loadView("wrapped");
}

init();
