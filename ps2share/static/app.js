/* PS2 File Share — UI.
   No build step and no framework: the server ships this file as-is. */
(() => {
"use strict";

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const SVG_NS = "http://www.w3.org/2000/svg";

const state = {
  server: null,     // /api/state
  live: null,       // /api/live
  games: null,
  usage: null,
  tab: "dashboard",
  pickerPath: "/",
  dirty: false,     // the share form has unsaved edits
  throughputTable: false,
  dailyTable: false,
  hover: null,
};

/* ── formatting ──────────────────────────────────────────────────── */

const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

function fmtBytes(value) {
  let size = Number(value) || 0;
  let i = 0;
  while (size >= 1024 && i < UNITS.length - 1) { size /= 1024; i += 1; }
  if (i === 0) return `${Math.round(size)} ${UNITS[i]}`;
  if (size >= 100) return `${size.toFixed(0)} ${UNITS[i]}`;
  if (size >= 10) return `${size.toFixed(1)} ${UNITS[i]}`;
  return `${size.toFixed(2)} ${UNITS[i]}`;
}

const fmtRate = (value) => `${fmtBytes(value)}/s`;

function fmtDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60), s = total % 60;
  if (m < 60) return `${m}m ${String(s).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

const fmtClock = (ts) =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

const fmtDay = (iso) => {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString([], { month: "short", day: "numeric" });
};

const escapeHtml = (text) =>
  String(text ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

/* ── api ─────────────────────────────────────────────────────────── */

async function request(path, options = {}) {
  const init = { headers: { "X-PS2Share": "1" }, ...options };
  if (options.body !== undefined) {
    init.method = options.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  let payload = {};
  try { payload = await response.json(); } catch { /* empty body */ }
  if (response.status === 401) {
    showLogin();
    throw new Error(payload.error || "Not signed in");
  }
  if (!response.ok) throw Object.assign(new Error(payload.error || `HTTP ${response.status}`), payload);
  return payload;
}

const get  = (path) => request(path);
const post = (path, body = {}) => request(path, { body });

/* ── toasts ──────────────────────────────────────────────────────── */

function toast(message, kind = "") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  $("#toasts").appendChild(node);
  setTimeout(() => node.remove(), kind === "bad" ? 7000 : 4000);
}

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      toast("Copied.", "ok");
      return;
    }
  } catch { /* fall through — plain HTTP has no clipboard API */ }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.cssText = "position:fixed;top:-1000px";
  document.body.appendChild(area);
  area.select();
  try { document.execCommand("copy"); toast("Copied.", "ok"); }
  catch { toast("Could not copy — select the text instead.", "bad"); }
  area.remove();
}

/* ── theme ───────────────────────────────────────────────────────── */

function initTheme() {
  const saved = localStorage.getItem("ps2share-theme") || "auto";
  document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const order = ["auto", "light", "dark"];
    const next = order[(order.indexOf(document.documentElement.dataset.theme) + 1) % order.length];
    document.documentElement.dataset.theme = next;
    localStorage.setItem("ps2share-theme", next);
    toast(`Theme: ${next}`);
    redrawCharts();
  });
}

/* ── tabs ────────────────────────────────────────────────────────── */

function initTabs() {
  $$("#tabs .tab").forEach((button) => {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
  });
  const fromHash = location.hash.replace("#", "");
  selectTab(["dashboard", "share", "games", "usage", "advanced"].includes(fromHash) ? fromHash : "dashboard");
}

function selectTab(name) {
  state.tab = name;
  location.hash = name;
  $$("#tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tabpanel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "games" && !state.games) refreshGames();
  if (name === "usage") refreshUsage();
  if (name === "advanced") { refreshPreview(); refreshLogs(); }
  redrawCharts();
}

/* ── login ───────────────────────────────────────────────────────── */

function showLogin() {
  $("#login").hidden = false;
  setTimeout(() => $("#login-password").focus(), 50);
}

function initLogin() {
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = $("#login-error");
    error.hidden = true;
    try {
      await post("/api/login", { password: $("#login-password").value });
      $("#login").hidden = true;
      $("#login-password").value = "";
      await refreshAll();
    } catch (exc) {
      error.textContent = exc.message || "Wrong password";
      error.hidden = false;
    }
  });
  $("#signout").addEventListener("click", async () => {
    await post("/api/logout").catch(() => {});
    location.reload();
  });
}

/* ── status pills & warnings ─────────────────────────────────────── */

function renderPills() {
  const s = state.server;
  if (!s) return;
  const pills = [];
  const smbd = s.samba.smbd || {};

  if (!s.samba.installed) {
    pills.push(["critical", "Samba not installed"]);
  } else if (smbd.active) {
    pills.push(["good", `Samba running${s.samba.version ? ` · ${s.samba.version.split("-")[0]}` : ""}`]);
  } else {
    pills.push(["critical", `Samba ${smbd.state || "stopped"}`]);
  }

  if (s.config.applied && s.include_present) {
    pills.push(["good", `Share: ${s.config.share_name}`]);
  } else {
    pills.push(["warning", "Share not set up"]);
  }

  pills.push([s.network.port_445 ? "good" : "warning", s.network.port_445 ? "Port 445 open" : "Port 445 closed"]);
  if (s.network.primary_ip) pills.push(["", s.network.primary_ip]);

  $("#status-pills").innerHTML = pills
    .map(([kind, label]) => `<span class="pill ${kind}"><span class="dot"></span>${escapeHtml(label)}</span>`)
    .join("");

  $("#signout").hidden = !s.config.ui_password_required;
  $("#brand-sub").textContent = s.config.share_path || "Open PS2 Loader over SMB";
}

const WARN_ICON = `<svg class="mark" viewBox="0 0 24 24" aria-hidden="true" fill="none"><path d="M12 9v4m0 4h.01M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

function renderWarnings() {
  const items = state.server?.warnings || [];
  $("#warnings").innerHTML = items
    .map((text) => {
      const severe = /not installed|no password|not listening|nothing is listening/i.test(text);
      return `<div class="banner ${severe ? "critical" : "warning"}">${WARN_ICON}<span>${escapeHtml(text)}</span></div>`;
    })
    .join("");
}

/* ── dashboard tiles ─────────────────────────────────────────────── */

function renderTiles() {
  const live = state.live;
  const server = state.server;

  if (live) {
    const rate = live.current.total;
    const [value, unit] = fmtRate(rate).split(" ");
    $("#tile-rate").innerHTML = `${escapeHtml(value)}<span class="hero-unit"> ${escapeHtml(unit)}</span>`;
    $("#tile-rate-sub").textContent = rate > 1024
      ? `${fmtRate(live.current.tx)} to the console · ${fmtRate(live.current.rx)} back`
      : "idle";

    const active = live.active_clients;
    $("#tile-clients").textContent = String(active);
    const known = live.clients.length;
    $("#tile-clients-sub").textContent = active
      ? `${live.clients.filter((c) => c.active).map((c) => c.ip).join(", ")}`
      : (known ? `${known} seen recently` : "no active connections");
  }

  if (state.usage) {
    $("#tile-today").textContent = state.usage.today.total_h;
  }

  const disk = server?.health?.disk;
  if (disk && disk.total) {
    $("#tile-free").textContent = fmtBytes(disk.free);
    $("#tile-free-sub").textContent = `${fmtBytes(disk.used)} used of ${fmtBytes(disk.total)} · ${disk.percent}% full`;
  } else if (server) {
    $("#tile-free").textContent = "—";
    $("#tile-free-sub").textContent = server.config.share_path ? "unknown" : "pick a folder first";
  }
}

/* ── connections ─────────────────────────────────────────────────── */

function renderConnections() {
  const live = state.live;
  const host = $("#connections");
  if (!live || !live.clients.length) {
    host.innerHTML = `<div class="empty">No console has connected yet.<br>Once OPL connects you will see its address, speed and the game it is reading.</div>`;
    return;
  }

  host.innerHTML = live.clients.map((client) => {
    const rate = client.tx_rate + client.rx_rate;
    const files = (client.files || []).map((file) => `
      <div class="file-row">
        <div class="file-head">
          <span class="file-name">${escapeHtml(file.name)}</span>
          <span class="file-pos">${file.has_position
            ? `${escapeHtml(fmtBytes(file.position))} of ${escapeHtml(file.size_h)} · ${file.percent}%`
            : `open · ${escapeHtml(file.size_h)}`}</span>
        </div>
        ${file.has_position
          ? `<div class="bar"><i style="width:${Math.max(0.5, Math.min(100, file.percent))}%"></i></div>`
          : ""}
      </div>`).join("");

    return `
      <div class="conn">
        <div>
          <div class="conn-head">
            <span class="conn-ip">${escapeHtml(client.ip)}</span>
            ${client.active
              ? `<span class="tag">${client.connections} connection${client.connections === 1 ? "" : "s"}</span>`
              : `<span class="tag">disconnected</span>`}
          </div>
          <div class="conn-meta">
            connected ${escapeHtml(fmtDuration(client.duration))} ·
            ${escapeHtml(client.total_h)} transferred ·
            peak ${escapeHtml(client.peak_h)}
          </div>
        </div>
        <div class="conn-rate ${rate > 1024 ? "" : "idle"}">${escapeHtml(fmtRate(rate))}</div>
        ${files ? `<div class="conn-files">${files}</div>` : ""}
      </div>`;
  }).join("");
}

/* ── OPL settings card ───────────────────────────────────────────── */

function renderOplSettings() {
  const s = state.server;
  if (!s) return;
  const opl = s.opl;
  const rows = [
    ["SMB Server IP", opl.server_ip || "(no address found)"],
    ["Port", String(opl.server_port)],
    ["Share name", opl.share_name],
    ["User", opl.user],
    ["Password", opl.password],
    ["Address type", opl.address_type],
  ];
  $("#opl-settings").innerHTML = `
    <dl class="kv">
      ${rows.map(([key, value]) => `
        <dt>${escapeHtml(key)}</dt>
        <dd>${escapeHtml(value)}<button class="btn ghost small copy" data-copy="${escapeHtml(value)}">Copy</button></dd>`).join("")}
    </dl>
    <ol class="steps">${opl.steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol>`;

  $$("#opl-settings .copy").forEach((button) =>
    button.addEventListener("click", () => copyText(button.dataset.copy)));
}

/* ── throughput chart ────────────────────────────────────────────── */

function el(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

/* Axis maxima are chosen in the unit the labels will be drawn in (KB, MB, …)
   so ticks read 2/4/6/8 MB/s rather than 1.79/3.58/5.36/7.15 MB/s. */
function niceCeil(value) {
  if (!(value > 0)) return 1024;
  let unit = 1;
  while (value / unit >= 1024 && unit < 1024 ** 5) unit *= 1024;
  const mantissa = value / unit;
  // every step divides by 4 cleanly, so the four gridline labels stay round
  for (const step of [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768]) {
    if (mantissa <= step) return step * unit;
  }
  return 1024 * unit;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function drawThroughput() {
  const svg = $("#throughput");
  const wrap = $("#throughput-wrap");
  const history = state.live?.history || [];
  const width = wrap.clientWidth;
  const height = wrap.clientHeight;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = "";
  if (!width || !height) return;

  const margin = { top: 12, right: 76, bottom: 22, left: 58 };
  const plotW = Math.max(10, width - margin.left - margin.right);
  const plotH = Math.max(10, height - margin.top - margin.bottom);
  const x0 = margin.left;
  const y0 = margin.top;

  if (history.length < 2) {
    svg.appendChild(Object.assign(el("text", {
      x: width / 2, y: height / 2, "text-anchor": "middle", fill: cssVar("--muted"), "font-size": 13,
    }), { textContent: "Waiting for traffic…" }));
    return;
  }

  const windowSeconds = Math.max(60, (state.server?.config.history_seconds) || 300);
  const now = history[history.length - 1][0];
  const start = now - windowSeconds;
  const peak = history.reduce((max, [, tx, rx]) => Math.max(max, tx, rx), 0);
  const yMax = niceCeil(Math.max(peak, 64 * 1024));

  const sx = (ts) => x0 + ((ts - start) / windowSeconds) * plotW;
  const sy = (value) => y0 + plotH - (value / yMax) * plotH;

  // grid + y ticks (hairlines, solid, one shade off the surface)
  const gridColour = cssVar("--grid");
  const mutedColour = cssVar("--muted");
  for (let i = 0; i <= 4; i += 1) {
    const value = (yMax / 4) * i;
    const y = sy(value);
    svg.appendChild(el("line", {
      x1: x0, x2: x0 + plotW, y1: y, y2: y,
      stroke: i === 0 ? cssVar("--axis") : gridColour, "stroke-width": 1,
    }));
    const label = el("text", {
      x: x0 - 8, y: y + 4, "text-anchor": "end", fill: mutedColour,
      "font-size": 11, "font-variant-numeric": "tabular-nums",
    });
    label.textContent = i === 0 ? "0" : fmtRate(value);
    svg.appendChild(label);
  }

  // x ticks, every minute back from now
  for (let back = 0; back <= windowSeconds; back += 60) {
    const x = sx(now - back);
    if (x < x0 - 1) continue;
    const label = el("text", {
      x, y: y0 + plotH + 16, "text-anchor": back === 0 ? "end" : "middle",
      fill: mutedColour, "font-size": 11,
    });
    label.textContent = back === 0 ? "now" : `−${back / 60}m`;
    svg.appendChild(label);
  }

  const series = [
    { key: 1, name: "To PS2", colour: cssVar("--series-1"), fill: cssVar("--series-1-fill") },
    { key: 2, name: "From PS2", colour: cssVar("--series-2"), fill: cssVar("--series-2-fill") },
  ];

  for (const spec of series) {
    const points = history.map((row) => [sx(row[0]), sy(row[spec.key])]);
    const line = points.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
    const baseline = sy(0);
    svg.appendChild(el("path", {
      d: `${line} L${points[points.length - 1][0].toFixed(1)},${baseline} L${points[0][0].toFixed(1)},${baseline} Z`,
      fill: spec.fill, stroke: "none",
    }));
    svg.appendChild(el("path", {
      d: line, fill: "none", stroke: spec.colour, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
  }

  // selective direct labels: the current value at the right-hand end only
  const lastRow = history[history.length - 1];
  const endLabels = series
    .map((spec) => ({ spec, value: lastRow[spec.key], y: sy(lastRow[spec.key]) }))
    .filter((item) => item.value > 0)
    .sort((a, b) => a.y - b.y);
  if (endLabels.length === 2 && endLabels[1].y - endLabels[0].y < 14) {
    endLabels[1].y = endLabels[0].y + 14;
  }
  for (const item of endLabels) {
    svg.appendChild(el("circle", {
      cx: sx(lastRow[0]), cy: sy(item.value), r: 3.5,
      fill: item.spec.colour, stroke: cssVar("--surface"), "stroke-width": 2,
    }));
    const label = el("text", {
      x: x0 + plotW + 8, y: Math.min(y0 + plotH, Math.max(y0 + 8, item.y + 4)),
      fill: item.spec.colour, "font-size": 11, "font-weight": 600,
      "font-variant-numeric": "tabular-nums",
    });
    label.textContent = fmtRate(item.value);
    svg.appendChild(label);
  }

  // crosshair
  if (state.hover && state.hover.chart === "throughput") {
    const index = state.hover.index;
    const row = history[index];
    if (row) {
      const x = sx(row[0]);
      svg.appendChild(el("line", {
        x1: x, x2: x, y1: y0, y2: y0 + plotH, stroke: cssVar("--axis"), "stroke-width": 1,
      }));
      for (const spec of series) {
        svg.appendChild(el("circle", {
          cx: x, cy: sy(row[spec.key]), r: 4,
          fill: spec.colour, stroke: cssVar("--surface"), "stroke-width": 2,
        }));
      }
    }
  }

  renderThroughputLegend(lastRow);
  svg.setAttribute("aria-label",
    `Throughput over the last ${Math.round(windowSeconds / 60)} minutes. ` +
    `Now ${fmtRate(lastRow[1])} to the PS2 and ${fmtRate(lastRow[2])} from it.`);
}

function renderThroughputLegend(lastRow) {
  const items = [
    ["--series-1", "To PS2", lastRow ? lastRow[1] : 0],
    ["--series-2", "From PS2", lastRow ? lastRow[2] : 0],
  ];
  $("#throughput-legend").innerHTML = items.map(([token, name, value]) => `
    <span class="legend-item">
      <span class="legend-swatch" style="background:var(${token})"></span>
      ${escapeHtml(name)} <span class="legend-value">${escapeHtml(fmtRate(value))}</span>
    </span>`).join("");
}

function throughputTable() {
  const history = state.live?.history || [];
  if (!history.length) return `<div class="empty">Nothing recorded yet.</div>`;
  const step = Math.max(1, Math.ceil(history.length / 40));
  const rows = history.filter((_, i) => i % step === 0).reverse();
  return `<div class="table-scroll"><table>
    <thead><tr><th>Time</th><th class="num">To PS2</th><th class="num">From PS2</th></tr></thead>
    <tbody>${rows.map(([ts, tx, rx]) => `
      <tr><td>${escapeHtml(fmtClock(ts))}</td>
          <td class="num">${escapeHtml(fmtRate(tx))}</td>
          <td class="num">${escapeHtml(fmtRate(rx))}</td></tr>`).join("")}
    </tbody></table></div>`;
}

function initThroughputInteraction() {
  const wrap = $("#throughput-wrap");
  const tip = $("#throughput-tip");

  const move = (event) => {
    const history = state.live?.history || [];
    if (history.length < 2) return;
    const rect = wrap.getBoundingClientRect();
    const margin = { left: 58, right: 76 };
    const plotW = Math.max(10, rect.width - margin.left - margin.right);
    const windowSeconds = Math.max(60, (state.server?.config.history_seconds) || 300);
    const now = history[history.length - 1][0];
    const ts = now - windowSeconds + ((event.clientX - rect.left - margin.left) / plotW) * windowSeconds;

    let best = 0;
    let bestGap = Infinity;
    history.forEach((row, i) => {
      const gap = Math.abs(row[0] - ts);
      if (gap < bestGap) { bestGap = gap; best = i; }
    });
    state.hover = { chart: "throughput", index: best };

    const row = history[best];
    const x = margin.left + ((row[0] - (now - windowSeconds)) / windowSeconds) * plotW;
    tip.hidden = false;
    tip.innerHTML = `
      <div class="tip-time">${escapeHtml(fmtClock(row[0]))}</div>
      <div class="tip-row"><span class="legend-swatch" style="background:var(--series-1)"></span>To PS2<span class="tip-val">${escapeHtml(fmtRate(row[1]))}</span></div>
      <div class="tip-row"><span class="legend-swatch" style="background:var(--series-2)"></span>From PS2<span class="tip-val">${escapeHtml(fmtRate(row[2]))}</span></div>`;
    tip.style.left = `${Math.max(70, Math.min(rect.width - 70, x))}px`;
    tip.style.top = `${Math.max(46, event.clientY - rect.top - 12)}px`;
    drawThroughput();
  };

  wrap.addEventListener("pointermove", move);
  wrap.addEventListener("pointerleave", () => {
    state.hover = null;
    tip.hidden = true;
    drawThroughput();
  });

  $("#throughput-table-toggle").addEventListener("click", (event) => {
    state.throughputTable = !state.throughputTable;
    event.currentTarget.setAttribute("aria-pressed", String(state.throughputTable));
    $("#throughput-wrap").hidden = state.throughputTable;
    const table = $("#throughput-table");
    table.hidden = !state.throughputTable;
    if (state.throughputTable) table.innerHTML = throughputTable();
  });
}

/* ── daily usage chart ───────────────────────────────────────────── */

function barPath(x, y, w, h, r) {
  const radius = Math.max(0, Math.min(r, w / 2, h));
  if (h <= 0.5) return "";
  return `M${x},${y + h} L${x},${y + radius} Q${x},${y} ${x + radius},${y} ` +
         `L${x + w - radius},${y} Q${x + w},${y} ${x + w},${y + radius} L${x + w},${y + h} Z`;
}

function drawDaily() {
  const svg = $("#daily");
  const wrap = $("#daily-wrap");
  const data = state.usage?.daily || [];
  const width = wrap.clientWidth;
  const height = wrap.clientHeight;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = "";
  if (!width || !height || !data.length) return;

  const margin = { top: 24, right: 12, bottom: 24, left: 58 };
  const plotW = Math.max(10, width - margin.left - margin.right);
  const plotH = Math.max(10, height - margin.top - margin.bottom);
  const x0 = margin.left;
  const y0 = margin.top;

  const peak = data.reduce((max, row) => Math.max(max, row.total), 0);
  if (peak <= 0) {
    const note = el("text", {
      x: width / 2, y: height / 2, "text-anchor": "middle", fill: cssVar("--muted"), "font-size": 13,
    });
    note.textContent = "No transfers recorded yet.";
    svg.appendChild(note);
    return;
  }

  const yMax = niceCeil(peak);
  const sy = (value) => y0 + plotH - (value / yMax) * plotH;
  const slot = plotW / data.length;
  const barWidth = Math.max(3, slot - 6);

  for (let i = 0; i <= 4; i += 1) {
    const value = (yMax / 4) * i;
    const y = sy(value);
    svg.appendChild(el("line", {
      x1: x0, x2: x0 + plotW, y1: y, y2: y,
      stroke: i === 0 ? cssVar("--axis") : cssVar("--grid"), "stroke-width": 1,
    }));
    const label = el("text", {
      x: x0 - 8, y: y + 4, "text-anchor": "end", fill: cssVar("--muted"),
      "font-size": 11, "font-variant-numeric": "tabular-nums",
    });
    label.textContent = i === 0 ? "0" : fmtBytes(value);
    svg.appendChild(label);
  }

  const labelEvery = Math.ceil(data.length / 7);
  const peakIndex = data.reduce((best, row, i) => (row.total > data[best].total ? i : best), 0);

  data.forEach((row, i) => {
    const x = x0 + i * slot + (slot - barWidth) / 2;
    const y = sy(row.total);
    const h = y0 + plotH - y;
    if (h > 0.5) {
      svg.appendChild(el("path", {
        d: barPath(x, y, barWidth, h, 4),
        fill: cssVar("--series-1"),
      }));
    }
    if (i % labelEvery === 0 || i === data.length - 1) {
      const label = el("text", {
        x: x + barWidth / 2, y: y0 + plotH + 16, "text-anchor": "middle",
        fill: cssVar("--muted"), "font-size": 11,
      });
      label.textContent = fmtDay(row.day);
      svg.appendChild(label);
    }
    if (i === peakIndex && row.total > 0) {
      const label = el("text", {
        x: x + barWidth / 2, y: Math.max(y0 - 6, y - 7), "text-anchor": "middle",
        fill: cssVar("--ink-2"), "font-size": 11, "font-weight": 600,
      });
      label.textContent = fmtBytes(row.total);
      svg.appendChild(label);
    }
    // a full-height, generously wide hit area so hovering never needs precision
    const hit = el("rect", {
      x: x0 + i * slot, y: y0, width: slot, height: plotH, fill: "transparent",
    });
    hit.addEventListener("pointerenter", (event) => showDailyTip(event, row));
    hit.addEventListener("pointerleave", () => { $("#daily-tip").hidden = true; });
    svg.appendChild(hit);
  });

  svg.setAttribute("aria-label",
    `Daily transfer over ${data.length} days. Busiest day ${data[peakIndex].day} at ${fmtBytes(data[peakIndex].total)}.`);
}

function showDailyTip(event, row) {
  const tip = $("#daily-tip");
  const rect = $("#daily-wrap").getBoundingClientRect();
  tip.hidden = false;
  tip.innerHTML = `
    <div class="tip-time">${escapeHtml(fmtDay(row.day))}</div>
    <div class="tip-row"><span class="legend-swatch" style="background:var(--series-1)"></span>Total<span class="tip-val">${escapeHtml(fmtBytes(row.total))}</span></div>
    <div class="tip-row">To PS2<span class="tip-val">${escapeHtml(fmtBytes(row.tx))}</span></div>
    <div class="tip-row">From PS2<span class="tip-val">${escapeHtml(fmtBytes(row.rx))}</span></div>`;
  tip.style.left = `${Math.max(80, Math.min(rect.width - 80, event.clientX - rect.left))}px`;
  tip.style.top = `${Math.max(60, event.clientY - rect.top - 12)}px`;
}

function dailyTable() {
  const data = (state.usage?.daily || []).slice().reverse();
  if (!data.length) return `<div class="empty">Nothing recorded yet.</div>`;
  return `<div class="table-scroll"><table>
    <thead><tr><th>Day</th><th class="num">To PS2</th><th class="num">From PS2</th><th class="num">Total</th></tr></thead>
    <tbody>${data.map((row) => `
      <tr><td>${escapeHtml(fmtDay(row.day))}</td>
          <td class="num">${escapeHtml(fmtBytes(row.tx))}</td>
          <td class="num">${escapeHtml(fmtBytes(row.rx))}</td>
          <td class="num">${escapeHtml(fmtBytes(row.total))}</td></tr>`).join("")}
    </tbody></table></div>`;
}

function redrawCharts() {
  if (state.tab === "dashboard" && !state.throughputTable) drawThroughput();
  if (state.tab === "usage" && !state.dailyTable) drawDaily();
}

/* ── share tab ───────────────────────────────────────────────────── */

function renderShareTab() {
  const s = state.server;
  if (!s || state.dirty) return;
  const cfg = s.config;

  $("#current-folder").textContent = cfg.share_path || "(not set)";
  $("#share-name").value = cfg.share_name;
  $("#workgroup").value = cfg.workgroup;
  $("#smb-username").value = cfg.username;
  $("#writable").checked = cfg.writable;
  $("#access-guest").checked = cfg.access_mode === "guest";
  $("#access-user").checked = cfg.access_mode === "user";
  $("#user-fields").hidden = cfg.access_mode !== "user";

  $("#opt-lanman").checked = cfg.lanman_auth;
  $("#opt-smb1only").checked = cfg.smb1_only;
  $("#opt-netbios").checked = cfg.enable_netbios;
  $("#opt-tuned").checked = cfg.tuned_performance;
  $("#opt-widelinks").checked = cfg.wide_links;

  renderFolderHealth();
}

function renderFolderHealth() {
  const health = state.server?.health;
  const host = $("#folder-health");
  if (!health || !state.server.config.share_path) { host.innerHTML = ""; return; }

  const chips = (health.folders || []).map((folder) =>
    `<span class="tag ${folder.exists ? "" : "warn"}" title="${escapeHtml(folder.purpose)}">${folder.exists ? "✓" : "✗"} ${escapeHtml(folder.name)}</span>`).join("");

  const disk = health.disk || {};
  const lines = [];
  if (disk.total) lines.push(`${fmtBytes(disk.free)} free of ${fmtBytes(disk.total)}${health.fstype ? ` · ${health.fstype}` : ""}`);
  health.problems?.forEach((text) => lines.push(`⚠ ${text}`));
  health.notes?.forEach((text) => lines.push(`• ${text}`));

  host.innerHTML = `
    <div class="folder-chips" style="margin-top:14px">${chips}</div>
    <p class="muted">${lines.map(escapeHtml).join("<br>")}</p>`;
}

function initShareTab() {
  const markDirty = () => { state.dirty = true; };
  ["#share-name", "#workgroup", "#smb-username", "#smb-password"].forEach((sel) =>
    $(sel).addEventListener("input", markDirty));
  ["#writable", "#create-user"].forEach((sel) => $(sel).addEventListener("change", markDirty));

  $$('input[name="access"]').forEach((radio) =>
    radio.addEventListener("change", () => {
      state.dirty = true;
      $("#user-fields").hidden = $("#access-user").checked === false;
    }));

  $("#choose-folder").addEventListener("click", () =>
    openPicker(state.server?.config.share_path || "/"));

  $("#apply-share").addEventListener("click", applyShare);

  $("#create-folders").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await post("/api/prepare", { path: state.server?.config.share_path });
      toast(result.message, "ok");
      await refreshState(true);
      renderFolderHealth();
    } catch (exc) { toast(exc.message, "bad"); }
    finally { button.disabled = false; }
  });

  $("#fix-permissions").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Working…";
    try {
      const result = await post("/api/permissions", {});
      toast(result.message, "ok");
      if (result.errors?.length) toast(`${result.errors.length} file(s) could not be changed.`, "bad");
    } catch (exc) { toast(exc.message, "bad"); }
    finally { button.disabled = false; button.textContent = "Fix permissions"; }
  });
}

async function applyShare() {
  const button = $("#apply-share");
  const host = $("#share-result");
  button.disabled = true;
  button.textContent = "Applying…";
  host.innerHTML = "";

  const body = {
    share_path: $("#current-folder").textContent.trim(),
    share_name: $("#share-name").value.trim(),
    workgroup: $("#workgroup").value.trim(),
    access_mode: $("#access-user").checked ? "user" : "guest",
    username: $("#smb-username").value.trim(),
    smb_password: $("#smb-password").value,
    create_user: $("#create-user").checked,
    writable: $("#writable").checked,
    lanman_auth: $("#opt-lanman").checked,
    smb1_only: $("#opt-smb1only").checked,
    enable_netbios: $("#opt-netbios").checked,
    tuned_performance: $("#opt-tuned").checked,
    wide_links: $("#opt-widelinks").checked,
    prepare_folders: true,
  };
  if (body.share_path === "(not set)") body.share_path = "";

  try {
    const result = await post("/api/share", body);
    host.innerHTML = `<div class="result ok"><strong>${escapeHtml(result.message)}</strong>${
      result.detail ? `<div class="detail">${escapeHtml(result.detail)}</div>` : ""}</div>`;
    toast(result.message, "ok");
    $("#smb-password").value = "";
    state.dirty = false;
    await refreshState(true);
  } catch (exc) {
    host.innerHTML = `<div class="result bad"><strong>${escapeHtml(exc.message)}</strong>${
      exc.detail ? `<div class="detail">${escapeHtml(exc.detail)}</div>` : ""}</div>`;
    toast(exc.message, "bad");
  } finally {
    button.disabled = false;
    button.textContent = "Apply & start sharing";
  }
}

/* ── folder picker ───────────────────────────────────────────────── */

const FOLDER_ICON = `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" stroke-width="1.6"/></svg>`;

function openPicker(path) {
  $("#picker").hidden = false;
  loadPicker(path || "/");
}

async function loadPicker(path) {
  state.pickerPath = path;
  let data;
  try { data = await get(`/api/fs?path=${encodeURIComponent(path)}`); }
  catch (exc) { toast(exc.message, "bad"); return; }

  $("#picker-disk").textContent = data.disk.total
    ? `${data.disk_h.free} free of ${data.disk_h.total}${data.writable ? "" : " · read-only"}`
    : "";

  $("#picker-suggestions").innerHTML = (data.suggestions || [])
    .map((item) => `<button class="chip" data-path="${escapeHtml(item.path)}">${escapeHtml(item.label)}</button>`).join("");

  $("#picker-crumbs").innerHTML = data.crumbs
    .map((crumb, i) => `${i ? '<span class="sep">/</span>' : ""}<button data-path="${escapeHtml(crumb.path)}">${escapeHtml(crumb.name)}</button>`).join("");

  const entries = data.entries || [];
  $("#picker-list").innerHTML = data.error
    ? `<div class="empty">${escapeHtml(data.error)}</div>`
    : (entries.length
        ? entries.map((entry) => `
            <div class="picker-item ${entry.readable ? "" : "locked"}" data-path="${escapeHtml(entry.path)}">
              ${FOLDER_ICON}<span>${escapeHtml(entry.name)}</span>
            </div>`).join("")
        : `<div class="empty">No sub-folders here. You can still use this folder.</div>`);

  $$("#picker-suggestions .chip, #picker-crumbs button").forEach((node) =>
    node.addEventListener("click", () => loadPicker(node.dataset.path)));
  $$("#picker-list .picker-item").forEach((node) =>
    node.addEventListener("click", () => {
      if (!node.classList.contains("locked")) loadPicker(node.dataset.path);
    }));
}

function initPicker() {
  $("#picker-close").addEventListener("click", () => { $("#picker").hidden = true; });
  $("#picker").addEventListener("click", (event) => {
    if (event.target.id === "picker") $("#picker").hidden = true;
  });
  $("#picker-use").addEventListener("click", () => {
    $("#current-folder").textContent = state.pickerPath;
    state.dirty = true;
    $("#picker").hidden = true;
    toast(`Selected ${state.pickerPath}. Press "Apply & start sharing" to use it.`);
  });
  $("#picker-mkdir").addEventListener("click", async () => {
    const name = $("#picker-new").value.trim();
    if (!name) return;
    const target = `${state.pickerPath.replace(/\/$/, "")}/${name}`;
    try {
      const result = await post("/api/mkdir", { path: target });
      if (!result.ok) throw new Error(result.message);
      $("#picker-new").value = "";
      toast(result.message, "ok");
      loadPicker(target);
    } catch (exc) { toast(exc.message, "bad"); }
  });
}

/* ── games tab ───────────────────────────────────────────────────── */

function renderGames() {
  const data = state.games;
  if (!data) return;

  const perFolder = data.games.reduce((acc, game) => {
    acc[game.folder] = (acc[game.folder] || 0) + 1;
    return acc;
  }, {});
  $("#games-summary").textContent = data.count
    ? `${data.count} disc image${data.count === 1 ? "" : "s"} · ${data.total_size_h} · ` +
      Object.entries(perFolder).map(([folder, n]) => `${n} in ${folder}`).join(", ")
    : "No disc images found yet.";

  $("#games-folders").innerHTML = (data.folders || [])
    .map((folder) => `<span class="tag ${folder.exists ? "" : "warn"}" title="${escapeHtml(folder.purpose)}">${folder.exists ? "✓" : "✗"} ${escapeHtml(folder.name)}</span>`)
    .join("");

  $("#games-list").innerHTML = data.games.length
    ? `<div class="table-scroll"><table>
        <thead><tr><th>Name</th><th>Folder</th><th class="num">Size</th><th>Game ID</th><th>Notes</th></tr></thead>
        <tbody>${data.games.map((game) => `
          <tr>
            <td>${escapeHtml(game.name)}</td>
            <td><span class="tag">${escapeHtml(game.folder)}</span></td>
            <td class="num">${escapeHtml(game.size_h)}</td>
            <td><code>${escapeHtml(game.game_id || "—")}</code></td>
            <td>${game.warnings.length
                  ? game.warnings.map((w) => `<div class="muted">⚠ ${escapeHtml(w)}</div>`).join("")
                  : '<span class="muted">ok</span>'}</td>
          </tr>`).join("")}
        </tbody></table></div>`
    : `<div class="empty">Drop your <code>.iso</code> files into the <code>DVD</code> folder of the share and press Rescan.</div>`;

  const cards = data.memory_cards || [];
  $("#vmc-list").innerHTML = cards.length
    ? `<table><thead><tr><th>File</th><th class="num">Size</th></tr></thead><tbody>${
        cards.map((card) => `<tr><td>${escapeHtml(card.name)}</td><td class="num">${escapeHtml(card.size_h)}</td></tr>`).join("")
      }</tbody></table>`
    : `<div class="empty">No memory card files yet.</div>`;
}

/* ── usage tab ───────────────────────────────────────────────────── */

function renderUsage() {
  const data = state.usage;
  if (!data) return;
  $("#usage-today").textContent = data.today.total_h;
  $("#usage-week").textContent = data.week.total_h;
  $("#usage-month").textContent = data.month.total_h;
  $("#usage-all").textContent = data.all_time.total_h;

  $("#usage-clients").innerHTML = data.clients.length
    ? `<table>
        <thead><tr><th>Console</th><th class="num">To PS2</th><th class="num">From PS2</th><th class="num">Total</th><th>Last seen</th></tr></thead>
        <tbody>${data.clients.map((row) => `
          <tr>
            <td><code>${escapeHtml(row.client)}</code></td>
            <td class="num">${escapeHtml(fmtBytes(row.tx))}</td>
            <td class="num">${escapeHtml(fmtBytes(row.rx))}</td>
            <td class="num">${escapeHtml(row.total_h)}</td>
            <td class="muted">${escapeHtml(new Date(row.last_seen * 1000).toLocaleString())}</td>
          </tr>`).join("")}
        </tbody></table>`
    : `<div class="empty">No traffic recorded in this period.</div>`;

  if (state.dailyTable) $("#daily-table").innerHTML = dailyTable();
  else drawDaily();
}

/* ── advanced tab ────────────────────────────────────────────────── */

function renderAdvanced() {
  const s = state.server;
  if (!s) return;
  const smbd = s.samba.smbd || {};
  $("#samba-detail").textContent = s.samba.installed
    ? `${s.samba.version || "Samba"} · unit ${smbd.unit || "unknown"} · ${smbd.state}${smbd.enabled ? " · starts at boot" : " · not enabled at boot"}`
    : "Samba is not installed on this server.";

  const firewall = s.firewall || {};
  $("#firewall-detail").textContent = firewall.tool
    ? `${firewall.tool}${firewall.active ? " (active)" : " (inactive)"}`
    : "No managed firewall detected.";
  $("#firewall-body").innerHTML = firewall.tool && firewall.active && !firewall.allows_smb
    ? `<p class="muted">SMB does not look allowed through ${escapeHtml(firewall.tool)}. Your PS2 will not reach the share until it is.</p>
       <div class="actions"><button class="btn primary" id="open-firewall">Allow SMB from my network</button></div>`
    : `<p class="muted">Nothing to do — SMB traffic is not being blocked by a firewall this app can see.</p>`;

  const openButton = $("#open-firewall");
  if (openButton) {
    openButton.addEventListener("click", async () => {
      openButton.disabled = true;
      try {
        const network = s.network.interfaces.find((i) => i.primary)?.network || "";
        const result = await post("/api/firewall", { network });
        toast(result.message, result.ok ? "ok" : "bad");
        await refreshState(true);
      } catch (exc) { toast(exc.message, "bad"); }
      finally { openButton.disabled = false; }
    });
  }

  $("#event-list").innerHTML = (s.events || []).length
    ? s.events.map((event) => `
        <div class="event ${escapeHtml(event.level)}">
          <time>${escapeHtml(fmtClock(event.ts))}</time>
          <span class="msg">${escapeHtml(event.message)}${event.detail ? ` — ${escapeHtml(event.detail)}` : ""}</span>
        </div>`).join("")
    : `<div class="empty">Nothing yet.</div>`;
}

function initAdvanced() {
  $$("[data-service]").forEach((button) =>
    button.addEventListener("click", async () => {
      const action = button.dataset.service;
      if (action === "stop" && !confirm("Stop Samba? Your PS2 will lose the share.")) return;
      button.disabled = true;
      try {
        const result = await post("/api/service", { action });
        toast(result.message || `smbd ${action}`, result.ok ? "ok" : "bad");
        await refreshState(true);
      } catch (exc) { toast(exc.message, "bad"); }
      finally { button.disabled = false; }
    }));

  $("#unshare").addEventListener("click", async () => {
    if (!confirm("Remove the PS2 share from Samba? Your files are not touched.")) return;
    try {
      const result = await post("/api/unshare", {});
      toast(result.message, result.ok ? "ok" : "bad");
      await refreshState(true);
    } catch (exc) { toast(exc.message, "bad"); }
  });

  $("#save-compat").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await post("/api/settings", {
        lanman_auth: $("#opt-lanman").checked,
        smb1_only: $("#opt-smb1only").checked,
        enable_netbios: $("#opt-netbios").checked,
        tuned_performance: $("#opt-tuned").checked,
        wide_links: $("#opt-widelinks").checked,
      });
      if (state.server?.config.applied) {
        const result = await post("/api/share", {});
        toast(result.message, result.ok ? "ok" : "bad");
      } else {
        toast("Saved. Apply the share to use these settings.", "ok");
      }
      await refreshState(true);
      refreshPreview();
    } catch (exc) { toast(exc.message, "bad"); }
    finally { button.disabled = false; }
  });

  $("#save-ui-password").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const next = $("#ui-new").value;
    if (!next && !confirm("Leave the web interface with no password at all?")) return;
    button.disabled = true;
    try {
      const result = await post("/api/ui-password", { current: $("#ui-current").value, password: next });
      toast(result.message, "ok");
      $("#ui-current").value = "";
      $("#ui-new").value = "";
      await refreshState(true);
    } catch (exc) { toast(exc.message, "bad"); }
    finally { button.disabled = false; }
  });

  $("#copy-config").addEventListener("click", () => copyText($("#config-preview").textContent));
  $("#refresh-logs").addEventListener("click", refreshLogs);
  $("#rescan").addEventListener("click", () => refreshGames(true));

  $("#daily-table-toggle").addEventListener("click", (event) => {
    state.dailyTable = !state.dailyTable;
    event.currentTarget.setAttribute("aria-pressed", String(state.dailyTable));
    $("#daily-wrap").hidden = state.dailyTable;
    const table = $("#daily-table");
    table.hidden = !state.dailyTable;
    if (state.dailyTable) table.innerHTML = dailyTable();
    else drawDaily();
  });
}

/* ── data refresh ────────────────────────────────────────────────── */

async function refreshState(force = false) {
  try {
    const payload = await get(`/api/state${force ? "?force=1" : ""}`);
    state.server = payload.state;
    renderPills();
    renderWarnings();
    renderShareTab();
    renderTiles();
    renderOplSettings();
    renderAdvanced();
  } catch { /* the error has already surfaced through request() */ }
}

async function refreshLive() {
  try {
    state.live = await get("/api/live");
    renderTiles();
    renderConnections();
    const source = { ss: "Measured per connection from kernel socket counters.",
                     fdinfo: "iproute2 (`ss`) is missing — rates are estimated from read progress.",
                     none: "No traffic source available yet." }[state.live.source] || "";
    $("#monitor-source").textContent = source;
    if (state.tab === "dashboard") {
      if (state.throughputTable) $("#throughput-table").innerHTML = throughputTable();
      else drawThroughput();
    }
  } catch { /* ignore a single failed poll */ }
}

async function refreshGames(force = false) {
  const button = $("#rescan");
  if (force) { button.disabled = true; button.textContent = "Scanning…"; }
  try {
    state.games = await get(`/api/games${force ? "?force=1" : ""}`);
    renderGames();
  } catch (exc) { toast(exc.message, "bad"); }
  finally { button.disabled = false; button.textContent = "Rescan"; }
}

async function refreshUsage() {
  try {
    state.usage = await get("/api/usage?days=14");
    renderUsage();
    renderTiles();
  } catch { /* ignore */ }
}

async function refreshPreview() {
  try {
    const payload = await get("/api/preview");
    $("#config-preview").textContent = payload.config;
    $("#preview-path").textContent = `${payload.include_path} — included from ${payload.smb_conf}`;
  } catch { /* ignore */ }
}

async function refreshLogs() {
  try {
    const payload = await get("/api/logs?lines=150");
    $("#log-text").textContent = payload.samba.text || "(empty)";
    $("#log-source").textContent = payload.samba.source || "";
  } catch { /* ignore */ }
}

async function refreshAll() {
  await refreshState(true);
  await Promise.all([refreshLive(), refreshUsage()]);
  if (state.tab === "games") refreshGames();
  if (state.tab === "advanced") { refreshPreview(); refreshLogs(); }
}

/* ── boot ────────────────────────────────────────────────────────── */

async function boot() {
  initTheme();
  initTabs();
  initLogin();
  initShareTab();
  initPicker();
  initAdvanced();
  initThroughputInteraction();

  let session;
  try { session = await (await fetch("/api/session")).json(); }
  catch { toast("Cannot reach the server.", "bad"); return; }

  if (session.auth_required && !session.authenticated) {
    showLogin();
    return;
  }
  await refreshAll();

  let tick = 0;
  setInterval(() => {
    if (document.hidden) return;
    tick += 1;
    const liveEvery = state.tab === "dashboard" ? 1 : 8;
    if (tick % liveEvery === 0) refreshLive();
    if (tick % 10 === 0) refreshState();
    if (tick % 30 === 0 && state.tab === "usage") refreshUsage();
  }, 1000);

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redrawCharts, 120);
  });
}

document.addEventListener("DOMContentLoaded", boot);
})();
