"use strict";
// Mai dashboard — vanilla JS, no external deps (100% local).

const MAX_POINTS = 60;
const series = { gpu: [], vram: [], ttft: [] };

// ---------- tabs ----------
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// ---------- controls ----------
async function post(url) {
  try {
    const r = await fetch(url, { method: "POST" });
    return await r.json();
  } catch (e) {
    return { ok: false, reason: String(e) };
  }
}
document.getElementById("btn-estop").addEventListener("click", () => post("/api/emergency_stop"));
document.getElementById("btn-resume").addEventListener("click", () => post("/api/resume"));

// ---------- tiny canvas line chart ----------
function drawChart(canvasId, data, color, maxHint) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (data.length < 2) return;
  const max = Math.max(maxHint || 1, ...data);
  const min = Math.min(0, ...data);
  const range = max - min || 1;
  const stepX = w / (MAX_POINTS - 1);

  // grid
  ctx.strokeStyle = "#2a2f3a"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = (h / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  // line
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
  data.forEach((v, i) => {
    const x = i * stepX;
    const y = h - ((v - min) / range) * (h - 10) - 5;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function pushPoint(arr, v) {
  arr.push(v);
  if (arr.length > MAX_POINTS) arr.shift();
}

// ---------- render ----------
function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }

function renderMetrics(m) {
  if (!m) return;
  setText("m-gpu", m.gpu_util_percent);
  setText("m-vram", m.vram_mb);
  setText("m-chat", m.chat_rate_per_min);
  pushPoint(series.gpu, m.gpu_util_percent);
  pushPoint(series.vram, m.vram_mb);
  drawChart("chart-gpu", series.gpu, "#5b9dff", 100);
  drawChart("chart-vram", series.vram, "#4caf50", 16384);
}

function renderLLM(l) {
  if (!l) return;
  setText("l-ttft", l.last_ttft_ms == null ? "–" : l.last_ttft_ms);
  setText("l-tps", l.last_decode_tps == null ? "–" : l.last_decode_tps);
  setText("l-req", l.requests_total);
  setText("l-parse", l.parse_rate_percent == null ? "–" : l.parse_rate_percent);
  setText("l-fb", l.fallback_total);
  if (l.last_ttft_ms != null) {
    pushPoint(series.ttft, l.last_ttft_ms);
    drawChart("chart-ttft", series.ttft, "#ffb454", 800);
  }
}

function renderState(s) {
  if (!s) return;
  const box = document.getElementById("state-name");
  box.textContent = s.current;
  box.className = "statebox " + s.current;
  setText("state-time", "(" + s.time_in_state_ms + " ms in state)");
  setText("state-interrupted", s.last_turn_interrupted ? "⚠ last turn interrupted" : "");
  const hist = document.getElementById("state-history");
  hist.innerHTML = "";
  (s.history || []).slice().reverse().forEach((h) => {
    const li = document.createElement("li");
    li.textContent = `${h.from} → ${h.to}  [${h.trigger}]  ${h.elapsed_ms}ms`;
    hist.appendChild(li);
  });
}

function renderFeatures(feats, vram) {
  if (!feats) return;
  if (vram) {
    document.getElementById("vram-budget").textContent =
      `VRAM budget: ${vram.used_mb} / ${vram.budget_mb} MB`;
  }
  const body = document.getElementById("features-body");
  body.innerHTML = "";
  feats.forEach((f) => {
    const tr = document.createElement("tr");
    const core = f.is_core ? '<span class="core-tag">CORE</span>' : "";
    const btn = f.is_core
      ? ""
      : `<button data-fid="${f.id}">${f.enabled ? "Disable" : "Enable"}</button>`;
    tr.innerHTML =
      `<td>${f.id}${core}</td><td>${f.category}</td><td>${f.vram_cost_mb}</td>` +
      `<td class="status-${f.status}">${f.status}</td><td>${btn}</td>`;
    body.appendChild(tr);
  });
  body.querySelectorAll("button[data-fid]").forEach((b) => {
    b.addEventListener("click", async () => {
      await post(`/api/features/${b.dataset.fid}/toggle`);
    });
  });
}

function renderTriggers(t) {
  if (!t) return;
  setText("t-size", t.size);
  setText("t-dropped", t.dropped_total);
  setText("t-expired", t.expired_total);
  const ul = document.getElementById("triggers-bytype");
  ul.innerHTML = "";
  const byType = t.by_type || {};
  if (Object.keys(byType).length === 0) {
    ul.innerHTML = "<li>(queue rỗng)</li>";
  } else {
    Object.entries(byType).forEach(([k, v]) => {
      const li = document.createElement("li");
      li.textContent = `${k}: ${v}`;
      ul.appendChild(li);
    });
  }
}

function render(snap) {
  renderMetrics(snap.metrics);
  renderLLM(snap.llm);
  renderState(snap.state);
  renderFeatures(snap.features, snap.vram);
  renderTriggers(snap.triggers);
}

// ---------- websocket ----------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  const conn = document.getElementById("conn");
  ws.onopen = () => { conn.textContent = "WS: connected"; conn.className = "badge on"; };
  ws.onclose = () => {
    conn.textContent = "WS: disconnected"; conn.className = "badge off";
    setTimeout(connect, 2000);
  };
  ws.onmessage = (ev) => {
    try { render(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
  };
}
connect();
