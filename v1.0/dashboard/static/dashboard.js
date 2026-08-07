"use strict";
// Mai dashboard — vanilla JS, no external deps (100% local).

const MAX_POINTS = 60;
const series = { gpu: [], vram: [], ttft: [], ttfa: [] };

// mood: 5 chiều, mỗi chiều 1 series rolling (pos). Target vẽ mức hiện tại dạng chấm.
const MOOD_DIMS = ["vui", "buon", "buc", "bon_chon", "nguong"];
const MOOD_COLORS = {
  vui: "#4caf50", buon: "#5b9dff", buc: "#e57373", bon_chon: "#ffb454", nguong: "#b39ddb",
};
const moodSeries = { vui: [], buon: [], buc: [], bon_chon: [], nguong: [] };

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

// ---------- Review tab (T3 rating + T7 correction) ----------
async function postJson(url, body) {
  try {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return await r.json();
  } catch (e) { return { ok: false, reason: String(e) }; }
}

function rate(rating) {
  const el = document.getElementById("rate-status");
  postJson("/api/rate", { rating }).then((r) => {
    el.textContent = r.ok ? `✓ ${rating} → turn #${r.turn_id}` : `✗ ${r.reason}`;
  });
}
document.getElementById("btn-rate-good").addEventListener("click", () => rate("good"));
document.getElementById("btn-rate-bad").addEventListener("click", () => rate("bad"));
document.getElementById("btn-rate-flag").addEventListener("click", () => rate("flag"));

async function loadRecentTurns() {
  const list = document.getElementById("review-list");
  list.innerHTML = "Đang tải…";
  let data;
  try { data = (await (await fetch("/api/recent_turns?n=20")).json()).turns; }
  catch (e) { list.innerHTML = "Lỗi tải: " + e; return; }
  if (!data || !data.length) { list.innerHTML = "<em>Chưa có turn nào.</em>"; return; }
  list.innerHTML = "";
  data.reverse().forEach((t) => {   // mới nhất trên cùng
    const div = document.createElement("div");
    div.className = "review-item";
    div.innerHTML =
      `<div class="review-meta">#${t.turn_id} · ${t.kind}</div>` +
      (t.user_text ? `<div class="review-user">👤 ${escapeHtml(t.user_text)}</div>` : "") +
      `<textarea class="review-edit">${escapeHtml(t.mai_text || "")}</textarea>` +
      `<div>` +
      `<button class="btn-item-good">👍</button>` +
      `<button class="btn-item-bad">👎</button>` +
      `<button class="btn-item-flag">🚩</button>` +
      `<button class="btn-save-correct">💾 Lưu sửa</button>` +
      `<button class="btn-skip">Bỏ qua</button>` +
      `<span class="save-status"></span></div>`;
    const ta = div.querySelector(".review-edit");
    const status = div.querySelector(".save-status");
    // xử xong 1 item → mờ dần rồi bỏ khỏi list (hàng đợi việc)
    const done = () => { div.style.opacity = "0.4"; setTimeout(() => div.remove(), 350); };
    const rateItem = (rating) => {
      postJson("/api/rate", { turn_id: t.turn_id, rating }).then((r) => {
        status.textContent = r.ok ? ` ✓ ${rating}` : " ✗ " + r.reason;
        if (r.ok) done();
      });
    };
    div.querySelector(".btn-item-good").addEventListener("click", () => rateItem("good"));
    div.querySelector(".btn-item-bad").addEventListener("click", () => rateItem("bad"));
    div.querySelector(".btn-item-flag").addEventListener("click", () => rateItem("flag"));
    div.querySelector(".btn-skip").addEventListener("click", done);
    div.querySelector(".btn-save-correct").addEventListener("click", () => {
      postJson("/api/correct", { turn_id: t.turn_id, corrected_text: ta.value }).then((r) => {
        status.textContent = r.ok ? " ✓ đã lưu" : " ✗ " + r.reason;
        if (r.ok) done();   // sửa xong cũng rớt khỏi hàng đợi
      });
    });
    list.appendChild(div);
  });
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
document.getElementById("btn-review-refresh").addEventListener("click", loadRecentTurns);
// auto-load khi mở tab Review
document.querySelector('[data-tab="review"]').addEventListener("click", loadRecentTurns);

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

// ---------- mood multi-line chart (y cố định 0-10) ----------
function drawMoodChart(canvasId, seriesMap, colors, targetMap) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const range = 10, stepX = w / (MAX_POINTS - 1);
  const yOf = (v) => h - (v / range) * (h - 10) - 5;

  ctx.strokeStyle = "#2a2f3a"; ctx.lineWidth = 1;
  for (let i = 0; i <= 5; i++) {
    const y = (h / 5) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  MOOD_DIMS.forEach((dim) => {
    const data = seriesMap[dim] || [];
    if (data.length >= 2) {
      ctx.strokeStyle = colors[dim]; ctx.lineWidth = 2; ctx.beginPath();
      data.forEach((v, i) => {
        const x = i * stepX, y = yOf(v);
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    if (targetMap && targetMap[dim] != null) {
      const y = yOf(Number(targetMap[dim]));
      ctx.strokeStyle = colors[dim]; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
      ctx.setLineDash([]);
    }
  });
}

function renderMood(mood) {
  if (!mood) return;
  const cur = mood.current_mood || {};
  const tgt = mood.mood_target || {};
  MOOD_DIMS.forEach((dim) => pushPoint(moodSeries[dim], Number(cur[dim] || 0)));
  drawMoodChart("chart-mood", moodSeries, MOOD_COLORS, tgt);

  const legend = document.getElementById("mood-legend");
  if (legend) {
    legend.innerHTML = "";
    MOOD_DIMS.forEach((dim) => {
      const t = tgt[dim] != null ? ` →${Number(tgt[dim]).toFixed(1)}` : "";
      const span = document.createElement("span");
      span.className = "mood-key";
      span.style.cssText = "display:inline-flex;align-items:center;gap:6px;margin:0 14px 6px 0;font-size:13px;";
      span.innerHTML =
        `<i style="width:12px;height:12px;border-radius:3px;display:inline-block;background:${MOOD_COLORS[dim]}"></i>` +
        `${dim}: <b>${Number(cur[dim] || 0).toFixed(1)}</b>${t}`;
      legend.appendChild(span);
    });
  }
  const flags = document.getElementById("mood-flags");
  if (flags) {
    const fl = mood.active_flags || [];
    flags.textContent = fl.length ? "tone flags: " + fl.join(", ") : "";
  }
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

function renderState(s, watchdog) {
  if (watchdog) {
    const last = watchdog.last_deadlock_state ? ` (last: ${watchdog.last_deadlock_state})` : "";
    setText("watchdog-info", `Watchdog: ${watchdog.deadlocks_total} deadlock${last} · watching ${(watchdog.watched_states || []).join(", ")}`);
  }
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
  setText("t-skipped", t.skipped_total);
  setText("t-interrupt", t.interrupt_total);
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

function renderFilter(f) {
  if (!f) return;
  setText("f-checks", f.checks_total);
  setText("f-hits", f.hits_total);
  setText("f-rate", f.hit_rate_percent == null ? "–" : f.hit_rate_percent);
  const regen = f.regen || {};
  setText("f-regen-rec", regen.recovered_total || 0);
  setText("f-regen-ex", regen.exhausted_total || 0);
  setText("f-fo", (f.fail_open_total || 0) + (f.service_fail_open_total || 0));

  const byCat = document.getElementById("filter-bycat");
  byCat.innerHTML = "";
  const cats = f.by_category || {};
  if (Object.keys(cats).length === 0) {
    byCat.innerHTML = "<li>(chưa có hit)</li>";
  } else {
    Object.entries(cats).forEach(([k, v]) => {
      const li = document.createElement("li");
      li.textContent = `${k}: ${v}`;
      byCat.appendChild(li);
    });
  }

  const recent = document.getElementById("filter-recent");
  recent.innerHTML = "";
  (f.recent || []).forEach((r) => {
    const li = document.createElement("li");
    li.textContent = `[${(r.categories || []).join(", ")}] → ${r.action}`;
    recent.appendChild(li);
  });
}

function renderTTS(t) {
  if (!t) return;
  setText("tts-turns", t.turns_total);
  setText("tts-ttfa", t.last_ttfa_ms == null ? "–" : t.last_ttfa_ms);
  setText("tts-sub", t.subtitle_fallback_total);
  const pl = t.pipeline || {};
  setText("tts-sents", pl.sentences_total || 0);
  const svc = t.service || {};
  setText("tts-svc-req", svc.requests_total || 0);
  setText("tts-svc-err", svc.errors_total || 0);
  setText("tts-svc-ttfa", svc.last_ttfa_ms == null ? "–" : Math.round(svc.last_ttfa_ms));
  setText("tts-svc-rtf", svc.last_rtf == null ? "–" : svc.last_rtf.toFixed(3));
  const ply = t.player || {};
  setText("tts-ply-p", ply.chunks_played || 0);
  setText("tts-ply-d", ply.chunks_dropped || 0);
  setText("tts-ply-q", ply.queue_size || 0);
  setText("tts-ply-on", ply.is_playing ? "▶" : "–");
  if (t.last_ttfa_ms != null) {
    pushPoint(series.ttfa, t.last_ttfa_ms);
    drawChart("chart-ttfa", series.ttfa, "#e57373", 2000);
  }
}

function render(snap) {
  renderMetrics(snap.metrics);
  renderLLM(snap.llm);
  renderFilter(snap.filter);
  renderTTS(snap.tts);
  renderState(snap.state, snap.watchdog);
  renderFeatures(snap.features, snap.vram);
  renderTriggers(snap.triggers);
  renderMood(snap.mood);
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
