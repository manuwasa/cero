// cero dashboard frontend — vanilla JS, no build step.
//
// On load:
//   1. Open /ws/live (auto-reconnect on drop)
//   2. Pull initial state from REST endpoints
//   3. Re-fetch every 5s
//   4. Render bus events into the live-events panel as they arrive
//
// Buttons:
//   TRIP  → POST /api/trip
//   reset → POST /api/reset

const $ = (sel) => document.querySelector(sel);
const POLL_MS = 5000;

// ──────────────────────────────────────────────────────────────────────
// Rendering
// ──────────────────────────────────────────────────────────────────────

function fmt(n, digits = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function ageStr(tsMs) {
  if (!tsMs) return "—";
  const diff = (Date.now() - tsMs) / 1000;
  if (diff < 60)    return `${Math.floor(diff)}s`;
  if (diff < 3600)  return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

function pnlClass(v) {
  if (v > 0) return "pnl-pos";
  if (v < 0) return "pnl-neg";
  return "";
}

async function refreshAccount() {
  const r = await fetch("/api/account");
  if (!r.ok) return;
  const a = await r.json();
  $("#acc-equity").textContent  = `${fmt(a.equity)} ${a.quote_currency}`;
  $("#acc-balance").textContent = `${fmt(a.balance)} ${a.quote_currency}`;
  $("#acc-upnl").textContent    = fmt(a.unrealized_pnl);
  $("#acc-upnl").className      = pnlClass(a.unrealized_pnl);
  $("#acc-margin").textContent  = fmt(a.margin_used);
  $("#acc-source").textContent  = a.source;
}

async function refreshPnl() {
  const r = await fetch("/api/pnl");
  if (!r.ok) return;
  const p = await r.json();
  const today = $("#pnl-today");
  today.textContent = fmt(p.today_pnl);
  today.className   = pnlClass(p.today_pnl);
  $("#pnl-wl").textContent = `${p.today_wins} / ${p.today_losses} (of ${p.today_count})`;
  const all = $("#pnl-all");
  all.textContent = `${fmt(p.all_time_pnl)} (${p.all_time_count} trades)`;
  all.className   = pnlClass(p.all_time_pnl);
}

async function refreshReadiness() {
  const r = await fetch("/api/readiness");
  if (!r.ok) return;
  const rows = await r.json();
  const tbody = $("#readiness-table tbody");
  tbody.innerHTML = "";
  for (const row of rows) {
    const tr = document.createElement("tr");
    if (row.tier === null) {
      tr.innerHTML = `<td>${row.symbol}</td><td class="muted">—</td><td class="muted">—</td><td class="muted">—</td><td class="muted">—</td>`;
    } else {
      tr.innerHTML = `
        <td>${row.symbol}</td>
        <td class="tier-${row.tier}">${row.tier}</td>
        <td class="dir-${row.direction}">${row.direction}</td>
        <td>${row.score}</td>
        <td class="muted">${ageStr(row.ts)}</td>
      `;
    }
    tbody.appendChild(tr);
  }
}

async function refreshPositions() {
  const r = await fetch("/api/positions");
  if (!r.ok) return;
  const rows = await r.json();
  const tbody = $("#positions-table tbody");
  tbody.innerHTML = "";
  $("#positions-empty").classList.toggle("hidden", rows.length > 0);
  for (const p of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${p.symbol}</td>
      <td class="dir-${p.side === "long" ? "long" : "short"}">${p.side}</td>
      <td>${fmt(p.size, 6)}</td>
      <td>${fmt(p.entry_price)}</td>
      <td>${fmt(p.mark_price)}</td>
      <td class="${pnlClass(p.unrealized_pnl)}">${fmt(p.unrealized_pnl)}</td>
      <td class="muted">${p.stop_loss ? fmt(p.stop_loss) : "—"} / ${p.take_profit ? fmt(p.take_profit) : "—"}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function refreshTrip() {
  const r = await fetch("/api/trip");
  if (!r.ok) return;
  const t = await r.json();
  const banner = $("#trip-banner");
  if (t.tripped) {
    banner.textContent = `TRIPPED — ${t.reason}: ${t.detail}`;
    banner.classList.remove("hidden");
    $("#trip-status").textContent = `tripped: ${t.reason} — ${t.detail}`;
  } else {
    banner.classList.add("hidden");
    $("#trip-status").textContent = "not tripped";
  }
}

async function refreshStatus() {
  const r = await fetch("/api/status");
  if (!r.ok) {
    $("#status").textContent = "api unreachable";
    return;
  }
  const s = await r.json();
  $("#status").textContent =
    `${s.exchange}${s.testnet ? " (testnet)" : ""} · mode=${s.mode} · ${s.symbols.length} symbols`;
}

// ──────────────────────────────────────────────────────────────────────
// Equity chart
// ──────────────────────────────────────────────────────────────────────

let _equityChart = null;

function _ensureEquityChart() {
  if (_equityChart) return _equityChart;
  const ctx = $("#equity-chart").getContext("2d");
  _equityChart = new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets: [{
      label: "equity",
      data: [],
      borderColor: "#4ade80",
      backgroundColor: "rgba(74,222,128,0.08)",
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0.15,
      fill: true,
    }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor: "#14141c", borderColor: "#2a2a38", borderWidth: 1 },
      },
      scales: {
        x: {
          ticks: { color: "#888", maxTicksLimit: 6, font: { family: "ui-monospace", size: 10 } },
          grid:  { color: "#2a2a38" },
        },
        y: {
          ticks: { color: "#888", font: { family: "ui-monospace", size: 10 } },
          grid:  { color: "#2a2a38" },
        },
      },
    },
  });
  $("#equity-window").addEventListener("change", refreshEquity);
  return _equityChart;
}

async function refreshEquity() {
  const hours = $("#equity-window").value || "24";
  const r = await fetch(`/api/account/history?hours=${hours}&max_points=200`);
  if (!r.ok) return;
  const rows = await r.json();
  if (rows.length === 0) {
    $("#equity-last").textContent = "no snapshots yet";
    return;
  }

  const chart = _ensureEquityChart();
  chart.data.labels = rows.map(p => {
    const d = new Date(p.ts);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  });
  chart.data.datasets[0].data = rows.map(p => p.equity);
  chart.update("none");

  const last = rows[rows.length - 1];
  const first = rows[0];
  const change = last.equity - first.equity;
  const pct = first.equity ? (change / first.equity * 100) : 0;
  const sign = change >= 0 ? "+" : "";
  $("#equity-last").textContent =
    `${last.equity.toFixed(2)}  (${sign}${change.toFixed(2)}, ${sign}${pct.toFixed(2)}%)`;
  $("#equity-last").className = change >= 0 ? "pnl-pos" : "pnl-neg";
}

async function refreshNews() {
  const r = await fetch("/api/news?limit=15");
  if (!r.ok) return;
  const rows = await r.json();
  const list = $("#news-list");
  const empty = $("#news-empty");
  list.innerHTML = "";
  empty.classList.toggle("hidden", rows.length > 0);
  for (const item of rows) {
    const li = document.createElement("li");
    const when = new Date(item.ts).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
    const safeContent = item.content
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const titleHtml = item.url
      ? `<a href="${item.url}" target="_blank" rel="noopener">${safeContent}</a>`
      : safeContent;
    li.innerHTML = `<time>${when}</time><span class="source">${item.source}</span>${titleHtml}`;
    list.appendChild(li);
  }
}

async function refreshAll() {
  await Promise.all([
    refreshStatus(), refreshAccount(), refreshPnl(),
    refreshReadiness(), refreshPositions(), refreshTrip(),
    refreshChart(), refreshEquity(), refreshNews(),
  ]);
}

// ──────────────────────────────────────────────────────────────────────
// Price chart (Chart.js, lazy-init on first refreshChart)
// ──────────────────────────────────────────────────────────────────────

let _chart = null;
let _chartReady = false;

async function _populateChartSymbols() {
  if (_chartReady) return;
  const r = await fetch("/api/status");
  if (!r.ok) return;
  const s = await r.json();
  const sel = $("#chart-symbol");
  sel.innerHTML = "";
  for (const sym of s.symbols) {
    const opt = document.createElement("option");
    opt.value = sym;
    opt.textContent = sym;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", refreshChart);
  $("#chart-tf").addEventListener("change", refreshChart);
  _chartReady = true;
}

function _ensureChart() {
  if (_chart) return _chart;
  const ctx = $("#price-chart").getContext("2d");
  _chart = new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets: [{
      label: "close",
      data: [],
      borderColor: "#ff6b9d",
      backgroundColor: "rgba(255,107,157,0.08)",
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0.15,
      fill: true,
    }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor: "#14141c", borderColor: "#2a2a38", borderWidth: 1 },
      },
      scales: {
        x: {
          ticks: { color: "#888", maxTicksLimit: 6, font: { family: "ui-monospace", size: 10 } },
          grid:  { color: "#2a2a38" },
        },
        y: {
          ticks: { color: "#888", font: { family: "ui-monospace", size: 10 } },
          grid:  { color: "#2a2a38" },
        },
      },
    },
  });
  return _chart;
}

async function refreshChart() {
  await _populateChartSymbols();
  const sym = $("#chart-symbol").value;
  const tf = $("#chart-tf").value;
  if (!sym || !tf) return;
  const r = await fetch(`/api/candles/${encodeURIComponent(sym)}/${tf}?limit=120`);
  if (!r.ok) return;
  const candles = await r.json();
  if (candles.length === 0) return;

  const chart = _ensureChart();
  chart.data.labels = candles.map(c => {
    const d = new Date(c.open_time);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  });
  chart.data.datasets[0].data = candles.map(c => c.close);
  chart.update("none");

  const last = candles[candles.length - 1];
  $("#chart-last").textContent = `${last.close.toLocaleString(undefined, { maximumFractionDigits: 2 })} · ${candles.length} bars`;
}

// ──────────────────────────────────────────────────────────────────────
// Live events (WebSocket)
// ──────────────────────────────────────────────────────────────────────

function logEvent(topic, data) {
  const li = document.createElement("li");
  const now = new Date().toLocaleTimeString();
  const summary = summarize(topic, data);
  li.innerHTML = `<time>${now}</time><span class="topic">${topic}</span>${summary}`;
  const list = $("#events-list");
  list.insertBefore(li, list.firstChild);
  while (list.children.length > 50) list.removeChild(list.lastChild);
  // Any of these warrant a state refresh.
  refreshAll();
}

function summarize(topic, data) {
  if (topic === "signal:new" && data?.signal_id) {
    return `signal #${data.signal_id} on ${data.symbol}`;
  }
  if (topic === "trip:fired") {
    return `TRIPPED — ${data?.reason || ""}: ${data?.detail || ""}`;
  }
  return `<code>${JSON.stringify(data)}</code>`;
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/live`);
  ws.addEventListener("open", () => {
    console.log("ws connected");
  });
  ws.addEventListener("message", (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      logEvent(msg.topic, msg.data);
    } catch (e) {
      console.error("bad ws message", e);
    }
  });
  ws.addEventListener("close", () => {
    console.warn("ws closed — retrying in 3s");
    setTimeout(connectWS, 3000);
  });
  ws.addEventListener("error", () => ws.close());
}

// ──────────────────────────────────────────────────────────────────────
// Controls
// ──────────────────────────────────────────────────────────────────────

$("#btn-trip").addEventListener("click", async () => {
  if (!confirm("Fire TRIP? This cancels all orders and closes all positions.")) return;
  const r = await fetch("/api/trip", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ detail: "via dashboard" }),
  });
  if (r.ok) refreshAll();
});

$("#btn-reset").addEventListener("click", async () => {
  if (!confirm("Clear the active TRIP?")) return;
  const r = await fetch("/api/reset", { method: "POST" });
  if (r.ok) refreshAll();
});

// ──────────────────────────────────────────────────────────────────────
// Boot
// ──────────────────────────────────────────────────────────────────────

refreshAll();
setInterval(refreshAll, POLL_MS);
connectWS();
