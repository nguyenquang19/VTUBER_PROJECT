"use strict";

const ui = {
  snapshot: null,
  section: "overview",
  sourceMode: "auto",
  wsConnected: false,
  socket: null,
  connectGeneration: 0,
  reconnectTimer: null,
};
const sectionTitles = {
  overview: "Live cockpit",
  brain: "Mood & Brain",
  conversation: "Dữ liệu hội thoại",
  system: "Hệ thống",
  evaluation: "Đánh giá",
};
const moodAxes = [
  { key: "vui", label: "Vui", color: "#68e3b5" },
  { key: "buon", label: "Buồn", color: "#72a7ff" },
  { key: "buc", label: "Bực", color: "#ff716d" },
  { key: "bon_chon", label: "Bồn chồn", color: "#f4bd61" },
  { key: "nguong", label: "Ngượng", color: "#d99cff" },
];
const svgNamespace = "http://www.w3.org/2000/svg";
const radarState = new Map();
const byId = (id) => document.getElementById(id);
const text = (id, value, fallback = "—") => {
  const node = byId(id);
  if (node) node.textContent = value == null || value === "" ? fallback : String(value);
};
const clear = (node) => { while (node && node.firstChild) node.removeChild(node.firstChild); };
const div = (className, value) => {
  const node = document.createElement("div");
  node.className = className;
  if (value != null) node.textContent = String(value);
  return node;
};
const clampMood = (value) => Math.max(0, Math.min(10, Number(value) || 0));
const displayMs = (value) => value == null ? "—" : `${Math.round(Number(value))} ms`;

async function postJson(url, body = {}) {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return { status: response.status, ...(await response.json()) };
  } catch (error) {
    return { ok: false, reason: String(error) };
  }
}

function switchSection(name) {
  ui.section = name;
  document.querySelectorAll(".operator-nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.section === name);
  });
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("active", view.id === `section-${name}`);
  });
  text("page-title", sectionTitles[name]);
  if (name === "evaluation") loadReviewQueue();
  if (name === "conversation" && ui.sourceMode === "history") loadHistory();
}

document.querySelectorAll(".operator-nav button").forEach((button) => {
  button.addEventListener("click", () => switchSection(button.dataset.section));
});

function setListEmpty(node, message) {
  clear(node);
  node.classList.add("empty");
  node.textContent = message;
}

function addStackItem(node, title, detail, tone = "") {
  if (node.classList.contains("empty")) clear(node);
  node.classList.remove("empty");
  const row = div(`stack-item ${tone}`.trim());
  const heading = document.createElement("strong");
  heading.textContent = String(title);
  row.appendChild(heading);
  if (detail) row.appendChild(div("muted", detail));
  node.appendChild(row);
}

function addChip(node, value) {
  node.appendChild(div("chip", value));
}

function addThreadItem(node, thread) {
  if (node.classList.contains("empty")) clear(node);
  node.classList.remove("empty");
  const row = div(`stack-item thread-item status-${thread.status || "active"}`);
  const head = div("thread-head");
  const title = document.createElement("strong");
  title.textContent = thread.topic || thread.summary || thread.thread_id || "thread";
  head.append(title, div("thread-status", thread.status || "active"));
  row.appendChild(head);
  row.appendChild(div("muted", thread.summary || "Chưa có tóm tắt."));
  const meta = div("thread-meta");
  addChip(meta, thread.kind || "thread");
  addChip(meta, `next: ${thread.next_move || "none"}`);
  addChip(meta, `${Number(thread.move_count || 0)} moves`);
  row.appendChild(meta);
  if ((thread.open_questions || []).length) {
    const question = thread.open_questions.at(-1);
    row.appendChild(div("thread-question", `Đang chờ: ${question.text || question}`));
  }
  node.appendChild(row);
}

function sourceInfo(snapshot) {
  const value = snapshot.dashboard_source || {};
  if (value.actual) return value;
  const online = Boolean((snapshot.runtime || {}).online);
  return {
    requested: ui.sourceMode,
    actual: online ? "live" : "history",
    available: true,
    read_only: !online,
    sampled_at: snapshot.captured_at || null,
  };
}

function renderConnection(snapshot) {
  const node = byId("connection-status");
  const source = sourceInfo(snapshot);
  const runtimeOnline = Boolean((snapshot.runtime || {}).online);
  let label = "Mất kết nối";
  let tone = "critical";
  if (ui.wsConnected) {
    if (source.actual === "live" && source.available && runtimeOnline) {
      label = "Live realtime";
      tone = "ready";
    } else if (source.actual === "live" && !source.available) {
      label = "Live không khả dụng";
      tone = "critical";
    } else {
      label = "Dữ liệu lịch sử";
      tone = "warning";
    }
  }
  node.textContent = label;
  node.className = `pill ${tone}`;
  text("sidebar-source", label);
  text("system-source-detail", `${source.requested || "auto"} → ${source.actual || "unknown"}${source.read_only ? " · chỉ đọc" : ""}`);
  const sampled = source.sampled_at ? new Date(source.sampled_at) : null;
  text("source-freshness", sampled && !Number.isNaN(sampled.getTime()) ? `Mẫu ${sampled.toLocaleTimeString("vi-VN")}` : "Chưa có mẫu");
}

function setPill(id, value, tone = "neutral") {
  const node = byId(id);
  if (!node) return;
  node.textContent = value == null || value === "" ? "—" : String(value);
  node.className = `pill ${tone}`;
}

function renderOverview(snapshot) {
  const overview = snapshot.operator_overview || {};
  const runtime = snapshot.runtime || {};
  const operations = snapshot.operations || {};
  const agent = snapshot.agent || {};
  const source = sourceInfo(snapshot);
  const hero = byId("status-hero");
  hero.className = `status-ribbon ${overview.overall_status || "neutral"}`;
  text("overview-headline", overview.headline, "Đang chờ snapshot…");
  text("overview-action", overview.action_required, "Chưa có yêu cầu vận hành.");
  text("overview-runtime", overview.runtime_online ? "Online" : "Offline");
  text("overview-incidents", overview.unresolved_incidents || 0);
  setPill("overview-current-action", overview.current_action, "neutral");
  const deliveryTone = overview.current_delivery_state === "failed" ? "critical" : overview.current_delivery_state === "delivered" ? "ready" : "neutral";
  setPill("overview-delivery", overview.current_delivery_state, deliveryTone);
  text("live-speech", agent.last_spoken_summary, "Chưa có câu nói trong phiên này.");
  text("live-phase", agent.stream_phase);
  const topic = agent.current_topic || {};
  text("live-topic", topic.topic || topic.summary);
  text("live-reason", overview.current_reason);
  renderConnection(snapshot);

  const controls = Boolean(runtime.controls_available) && !source.read_only;
  byId("operator-emergency").disabled = !controls;
  byId("operator-pause").disabled = !controls || Boolean(operations.paused);
  byId("operator-resume").disabled = !controls || !operations.paused;

  const recovery = byId("overview-recovery");
  recovery.hidden = !overview.recovery_action;
  recovery.textContent = overview.recovery_action === "resume_agent" ? "Tiếp tục" : overview.recovery_action === "resume_emergency" ? "Mở lại output" : "Mở chi tiết";
  recovery.onclick = async () => {
    if (overview.recovery_action === "resume_agent") await postJson("/api/agent/resume", { reason: "operator dashboard recovery" });
    else if (overview.recovery_action === "resume_emergency") await postJson("/api/resume", {});
    else switchSection(overview.recovery_action === "inspect_decision" ? "brain" : "system");
  };

  const llm = snapshot.llm || {};
  const tts = snapshot.tts || {};
  const metrics = snapshot.metrics || {};
  text("live-llm-ttft", displayMs(llm.last_ttft_ms));
  text("live-tts-ttfa", displayMs(tts.last_ttfa_ms == null ? (tts.service || {}).last_ttfa_ms : tts.last_ttfa_ms));
  text("live-audio-queue", (tts.player || {}).queue_size);
  const gpu = metrics.gpu_util_percent == null ? "—" : `${metrics.gpu_util_percent}%`;
  const vram = metrics.vram_mb == null ? "—" : `${metrics.vram_mb} MB`;
  text("live-gpu", `${gpu} / ${vram}`);

  const healthTargets = Object.values((snapshot.health_supervisor || {}).targets || {});
  const unhealthy = healthTargets.filter((value) => value.health !== "healthy" || value.circuit_open);
  const healthSummary = byId("live-health");
  healthSummary.className = `health-summary ${unhealthy.length ? "warning" : healthTargets.length ? "ready" : "neutral"}`;
  healthSummary.lastElementChild.textContent = unhealthy.length ? `${unhealthy.length} service cần kiểm tra` : healthTargets.length ? `${healthTargets.length} service ổn định` : "Chưa có health snapshot";

  const threads = agent.open_threads || [];
  const overviewThreads = byId("overview-threads");
  setListEmpty(overviewThreads, "Không có thread mở.");
  threads.forEach((thread) => addThreadItem(overviewThreads, thread));
  setPill("live-thread-count", `${threads.length} thread`, threads.length ? "ready" : "neutral");

  const queue = operations.action_queue || [];
  const overviewActions = byId("overview-actions");
  setListEmpty(overviewActions, "Queue trống.");
  queue.slice(0, 4).forEach((item) => addStackItem(
    overviewActions,
    item.kind || "action",
    item.id || item.status || `${item.pending_count || 0} pending`,
  ));
  setPill("live-queue-count", queue.length, queue.length ? "warning" : "neutral");

  const health = byId("overview-health");
  setListEmpty(health, "Không có service cần chú ý.");
  (overview.unhealthy_services || []).slice(0, 3).forEach((service) => addStackItem(health, service, "Cần kiểm tra recovery", "failed"));
  const incidents = byId("overview-incident-list");
  setListEmpty(incidents, "Không có incident.");
  (((snapshot.incidents || {}).recent) || []).slice(-2).reverse().forEach((item) => {
    addStackItem(incidents, `${item.severity || "warning"} · ${item.component || "unknown"}`, item.action || item.summary || item.status, "failed");
  });

  renderDecision(snapshot.decisions);
  renderGoal(overview.active_goal, controls);
}

function renderDecision(decisions) {
  const current = (decisions || {}).current || {};
  text("decision-action", current.action);
  text("decision-reason", current.reason);
  text("decision-id", current.decision_id);
  text("decision-transaction", current.transaction_id || current.transaction_state);
  const tone = current.delivery_state === "failed" ? "critical" : ["committed", "completed"].includes(current.outcome) ? "ready" : "neutral";
  setPill("decision-outcome", current.outcome, tone);
  const evidence = byId("decision-evidence");
  clear(evidence);
  (current.evidence_refs || []).forEach((value) => addChip(evidence, value));
  if (!evidence.children.length) evidence.appendChild(div("muted", "Chưa có evidence."));
}

function renderGoal(goal, controls) {
  const host = byId("overview-goal");
  clear(host);
  if (!goal) {
    host.className = "empty";
    host.textContent = "Chưa có goal active.";
  } else {
    host.className = "stack-list";
    addStackItem(host, `${goal.kind || "goal"} · P${goal.priority || 0}`, goal.reason || goal.id || goal.goal_id);
    const actions = div("button-row");
    [["Hoàn thành", "complete"], ["Hủy", "cancel"]].forEach(([label, action]) => {
      const button = document.createElement("button");
      button.className = action === "cancel" ? "button danger-soft" : "button";
      button.textContent = label;
      button.disabled = !controls;
      button.addEventListener("click", () => postJson(`/api/goals/${encodeURIComponent(goal.id || goal.goal_id)}/${action}`, { reason: "operator dashboard" }));
      actions.appendChild(button);
    });
    host.appendChild(actions);
  }
  byId("operator-goal-pin").disabled = !controls;
}

function svgNode(name, attributes = {}) {
  const node = document.createElementNS(svgNamespace, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function radarCoordinates(values, radius = 105) {
  return moodAxes.map((axis, index) => {
    const angle = (-90 + index * 72) * Math.PI / 180;
    const scaled = radius * clampMood(values[axis.key]) / 10;
    return [150 + Math.cos(angle) * scaled, 150 + Math.sin(angle) * scaled];
  });
}

function pointString(points) {
  return points.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
}

function buildRadar(svg) {
  if (svg.dataset.ready === "true") return;
  [2, 4, 6, 8, 10].forEach((level) => {
    const values = Object.fromEntries(moodAxes.map((axis) => [axis.key, level]));
    svg.appendChild(svgNode("polygon", { points: pointString(radarCoordinates(values)), class: "radar-grid" }));
  });
  moodAxes.forEach((axis, index) => {
    const angle = (-90 + index * 72) * Math.PI / 180;
    svg.appendChild(svgNode("line", { x1: 150, y1: 150, x2: 150 + Math.cos(angle) * 105, y2: 150 + Math.sin(angle) * 105, class: "radar-axis" }));
  });
  const target = svgNode("polygon", { class: "radar-target" });
  target.dataset.role = "target";
  const current = svgNode("polygon", { class: "radar-current" });
  current.dataset.role = "current";
  svg.append(target, current);
  moodAxes.forEach((axis, index) => {
    const angle = (-90 + index * 72) * Math.PI / 180;
    const label = svgNode("text", { x: 150 + Math.cos(angle) * 129, y: 150 + Math.sin(angle) * 129, class: "radar-label" });
    label.textContent = axis.label;
    svg.appendChild(label);
  });
  svg.dataset.ready = "true";
}

function animatePolygon(node, points) {
  const next = points;
  const previous = radarState.get(node) || next;
  radarState.set(node, next);
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    node.setAttribute("points", pointString(next));
    return;
  }
  const started = performance.now();
  const durationMs = 700;
  const step = (now) => {
    const progress = Math.min(1, (now - started) / durationMs);
    const eased = 1 - Math.pow(1 - progress, 3);
    const value = next.map(([x, y], index) => [
      previous[index][0] + (x - previous[index][0]) * eased,
      previous[index][1] + (y - previous[index][1]) * eased,
    ]);
    node.setAttribute("points", pointString(value));
    if (progress < 1 && radarState.get(node) === next) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderRadar(svgId, mood, target) {
  const svg = byId(svgId);
  buildRadar(svg);
  animatePolygon(svg.querySelector('[data-role="current"]'), radarCoordinates(mood));
  animatePolygon(svg.querySelector('[data-role="target"]'), radarCoordinates(target));
}

function renderMood(snapshot) {
  const moodSnapshot = snapshot.mood || {};
  const mood = moodSnapshot.mood_pos || moodSnapshot.current_mood || {};
  const target = moodSnapshot.mood_target || mood;
  const hasMood = moodAxes.some((axis) => mood[axis.key] != null);
  const source = sourceInfo(snapshot);
  const stale = !hasMood || !ui.wsConnected || source.actual !== "live";
  const dominant = moodAxes.reduce((best, axis) => clampMood(mood[axis.key]) > clampMood(mood[best.key]) ? axis : best, moodAxes[0]);
  ["mood-core-compact", "mood-core-detail"].forEach((id) => {
    const card = byId(id);
    card.classList.toggle("stale", stale);
    card.style.setProperty("--mood-accent", dominant.color);
  });
  renderRadar("mood-radar-compact", mood, target);
  renderRadar("mood-radar-detail", mood, target);

  const compact = byId("mood-compact-values");
  clear(compact);
  moodAxes.forEach((axis) => {
    const item = div("mood-mini");
    item.style.setProperty("--axis-color", axis.color);
    item.append(div("", axis.label), div("", hasMood && mood[axis.key] != null ? clampMood(mood[axis.key]).toFixed(1) : "—"));
    const valueNode = item.lastChild;
    valueNode.className = "mood-mini-value";
    valueNode.style.color = axis.color;
    valueNode.style.fontWeight = "700";
    compact.appendChild(item);
  });

  const detail = byId("brain-mood");
  clear(detail);
  detail.className = "mood-axis-list";
  moodAxes.forEach((axis) => {
    const current = clampMood(mood[axis.key]);
    const targetValue = mood[axis.key] == null ? 0 : clampMood(target[axis.key] == null ? current : target[axis.key]);
    const row = div("mood-axis");
    row.style.setProperty("--axis-color", axis.color);
    const label = document.createElement("span");
    label.textContent = axis.label;
    const track = div("mood-axis-track");
    const fill = div("mood-axis-fill");
    fill.style.width = `${current * 10}%`;
    const marker = div("mood-axis-target");
    marker.style.left = `calc(${targetValue * 10}% - 1px)`;
    track.append(fill, marker);
    const value = div("mood-axis-value", hasMood && mood[axis.key] != null ? `${current.toFixed(2)} → ${targetValue.toFixed(2)}` : "—");
    row.append(label, track, value);
    detail.appendChild(row);
  });
  if (!hasMood) setListEmpty(detail, "Chưa có mood snapshot.");

  const sampleTime = moodSnapshot.sampled_at ? new Date(moodSnapshot.sampled_at) : null;
  const meta = sampleTime && !Number.isNaN(sampleTime.getTime()) ? `${sampleTime.toLocaleTimeString("vi-VN")} · tick ${moodSnapshot.ticks == null ? "—" : moodSnapshot.ticks}` : "Snapshot tĩnh";
  text("mood-compact-meta", stale ? `${meta} · đứng` : meta);
  setPill("brain-mood-meta", stale ? `${meta} · đứng` : meta, stale ? "warning" : "ready");
}

function renderBrain(snapshot) {
  const decisions = snapshot.decisions || { recent: [] };
  const list = byId("brain-decisions");
  setListEmpty(list, "Chưa có quyết định.");
  (decisions.recent || []).forEach((record) => {
    const tone = record.delivery_state === "failed" ? "failed" : ["committed", "completed"].includes(record.outcome) ? "committed" : "";
    const row = div(`decision-item ${tone}`.trim());
    const title = document.createElement("strong");
    title.textContent = `${record.action || "wait"} · ${record.reason || "unknown"}`;
    row.append(title, div("muted mono", `${record.decision_id || ""} · ${record.transaction_state || record.delivery_state || "not_started"}`));
    const chips = div("chip-row");
    (record.evidence_refs || []).forEach((value) => addChip(chips, value));
    if (chips.children.length) row.appendChild(chips);
    list.classList.remove("empty");
    list.appendChild(row);
  });
  text("brain-record-count", (decisions.recent || []).length);
  const summary = (decisions.current || {}).candidate_summary || {};
  fillDl("brain-candidates", [
    ["Candidates", summary.candidate_count], ["Pool size", summary.pool_size],
    ["Pulse", summary.pulse_state], ["Top score", summary.top_score],
    ["Active goal", summary.active_goal_id], ["Safety hold", summary.safety_hold ? "yes" : "no"],
  ]);
  const thought = snapshot.thought_engine || {};
  setPill("thought-stage", thought.stage || "idle", thought.pending_interrupted ? "warning" : thought.stage ? "ready" : "neutral");
  fillDl("thought-engine", [["Nguyên nhân", thought.cause], ["Ý định", thought.intention], ["Pending", thought.pending_plan_id], ["Ledger", (thought.ledger || []).length]]);
  renderMood(snapshot);
}

function renderConversation(snapshot) {
  const agent = snapshot.agent || {};
  text("conversation-phase", agent.stream_phase);
  text("conversation-topic", (agent.current_topic || {}).topic || (agent.current_topic || {}).summary);
  text("conversation-last-speech", agent.last_spoken_summary);
  const threads = byId("conversation-threads");
  setListEmpty(threads, "Không có thread mở.");
  (agent.open_threads || []).forEach((thread) => addThreadItem(threads, thread));
  const actions = byId("conversation-actions");
  setListEmpty(actions, "Queue trống.");
  (((snapshot.operations || {}).action_queue) || []).forEach((item) => addStackItem(actions, item.kind || "action", item.id || item.status || `${item.pending_count || 0} pending`));
  text("conversation-environment", agent.environment_summary ? JSON.stringify(agent.environment_summary, null, 2) : "Chưa có environment.");
  const relationships = byId("conversation-relationships");
  setListEmpty(relationships, "Chưa có profile.");
  (((snapshot.relationships || {}).profiles) || []).forEach((profile) => addStackItem(relationships, profile.viewer_id || "pseudonym", `${profile.interaction_count || 0} interactions · ${profile.tone || "no tone"}`));
}

function addMetric(host, label, value) {
  const card = document.createElement("article");
  const name = document.createElement("span");
  const amount = document.createElement("strong");
  name.textContent = label;
  amount.textContent = value == null ? "—" : String(value);
  card.append(name, amount);
  host.appendChild(card);
}

function fillDl(id, pairs) {
  const host = byId(id);
  clear(host);
  pairs.forEach(([key, value]) => {
    const cell = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = key;
    dd.textContent = value == null || value === "" ? "—" : String(value);
    cell.append(dt, dd);
    host.appendChild(cell);
  });
}

function renderSystem(snapshot) {
  const metrics = snapshot.metrics || {};
  const llm = snapshot.llm || {};
  const tts = snapshot.tts || {};
  const filter = snapshot.filter || {};
  const metricHost = byId("system-metrics");
  clear(metricHost);
  const stale = metrics.gpu_metrics_stale ? " · stale" : "";
  addMetric(metricHost, "GPU", `${metrics.gpu_util_percent == null ? "—" : `${metrics.gpu_util_percent}%`}${stale}`);
  addMetric(metricHost, "VRAM", `${metrics.vram_mb == null ? "—" : `${metrics.vram_mb} / ${metrics.vram_total_mb == null ? "—" : metrics.vram_total_mb} MB`}${stale}`);
  addMetric(metricHost, "LLM TTFT", displayMs(llm.last_ttft_ms));
  addMetric(metricHost, "TTS TTFA", displayMs(tts.last_ttfa_ms == null ? (tts.service || {}).last_ttfa_ms : tts.last_ttfa_ms));
  const healthHost = byId("system-health");
  setListEmpty(healthHost, "Chưa có health snapshot.");
  Object.entries((snapshot.health_supervisor || {}).targets || {}).forEach(([name, value]) => addStackItem(healthHost, `${name} · ${value.health || "unknown"}`, value.message || value.last_action || "", value.circuit_open ? "failed" : ""));
  const featureHost = byId("system-features");
  setListEmpty(featureHost, "Chưa có feature snapshot.");
  const controls = Boolean((snapshot.runtime || {}).controls_available) && !sourceInfo(snapshot).read_only;
  (snapshot.features || []).forEach((feature) => {
    featureHost.classList.remove("empty");
    const row = div("stack-item");
    const label = document.createElement("strong");
    label.textContent = `${feature.id} · ${feature.status}`;
    row.appendChild(label);
    if (!feature.is_core) {
      const button = document.createElement("button");
      button.className = "button compact";
      button.textContent = feature.enabled ? "Tắt" : "Bật";
      button.disabled = !controls;
      button.addEventListener("click", () => postJson(`/api/features/${encodeURIComponent(feature.id)}/toggle`, {}));
      row.appendChild(button);
    }
    featureHost.appendChild(row);
  });
  fillDl("system-llm", [["Requests", llm.requests_total], ["Parse ok", `${llm.parse_rate_percent == null ? "—" : llm.parse_rate_percent}%`], ["Fallback", llm.fallback_total], ["Filter hits", filter.hits_total]]);
  fillDl("system-tts", [["Turns", tts.turns_total], ["Subtitle fallback", tts.subtitle_fallback_total], ["Played chunks", (tts.player || {}).chunks_played], ["Dropped chunks", (tts.player || {}).chunks_dropped]]);
}

function historyQuery() {
  const params = new URLSearchParams();
  const session = byId("history-session").value.trim();
  const kind = byId("history-kind").value;
  const delivered = byId("history-delivered").value;
  const started = byId("history-start").value;
  const ended = byId("history-end").value;
  const limit = byId("history-limit").value;
  if (session) params.set("session_id", session);
  if (kind) params.set("kind", kind);
  if (delivered) params.set("delivered", delivered);
  if (started) params.set("started_at", new Date(started).toISOString());
  if (ended) params.set("ended_at", new Date(ended).toISOString());
  if (limit) params.set("limit", limit);
  return params;
}

function renderHistory(value) {
  const host = byId("history-records");
  clear(host);
  host.classList.remove("empty");
  const records = value.records || [];
  const sessions = new Set();
  const kinds = new Set();
  records.forEach((record) => {
    if (record.session_id) sessions.add(record.session_id);
    if (record.kind || record.trigger_type) kinds.add(record.kind || record.trigger_type);
    const row = div("history-record");
    const parsedTime = record.timestamp ? new Date(record.timestamp) : null;
    row.appendChild(div("record-time", parsedTime && !Number.isNaN(parsedTime.getTime()) ? parsedTime.toLocaleString("vi-VN") : "Không có timestamp"));
    row.appendChild(div("record-kind", record.kind || record.trigger_type || "turn"));
    const copy = div("record-copy");
    const primary = document.createElement("strong");
    primary.textContent = record.mai_text || "Không có Mai text";
    copy.appendChild(primary);
    if (record.user_text) {
      const user = document.createElement("span");
      user.textContent = `Viewer: ${record.user_text}`;
      copy.appendChild(user);
    }
    row.appendChild(copy);
    const delivery = div(`delivery-mark ${record.delivered === true ? "yes" : record.delivered === false ? "no" : ""}`);
    delivery.textContent = record.delivered === true ? "Đã delivery" : record.delivered === false ? "Không delivery" : "Chưa có outcome";
    row.appendChild(delivery);
    host.appendChild(row);
  });
  if (!records.length) setListEmpty(host, "Không có record khớp bộ lọc.");
  setPill("history-summary", `${value.total_matched || 0} kết quả${value.malformed_skipped ? ` · bỏ ${value.malformed_skipped} dòng lỗi` : ""}`, value.malformed_skipped ? "warning" : "neutral");
  const options = byId("history-session-options");
  sessions.forEach((session) => {
    if ([...options.options].some((option) => option.value === session)) return;
    const option = document.createElement("option");
    option.value = session;
    options.appendChild(option);
  });
  const kindSelect = byId("history-kind");
  kinds.forEach((kind) => {
    if ([...kindSelect.options].some((option) => option.value === kind)) return;
    const option = document.createElement("option");
    option.value = kind;
    option.textContent = kind;
    kindSelect.appendChild(option);
  });
}

async function loadHistory() {
  const host = byId("history-records");
  setListEmpty(host, "Đang đọc journal…");
  try {
    const response = await fetch(`/api/history/turns?${historyQuery().toString()}`);
    const value = await response.json();
    if (!response.ok) throw new Error(value.reason || `HTTP ${response.status}`);
    renderHistory(value);
  } catch (error) {
    setListEmpty(host, `Không đọc được lịch sử: ${error}`);
    setPill("history-summary", "Không khả dụng", "critical");
  }
}

async function rateTurn(rating, turn = null) {
  const payload = turn ? { rating, turn_id: turn.turn_id, session_id: turn.session_id } : { rating };
  const result = await postJson("/api/rate", payload);
  text("evaluation-status", result.ok ? `Đã lưu ${rating}` : result.reason, "");
  if (result.ok && turn) loadReviewQueue();
}

async function correctTurn(turn, textarea) {
  const result = await postJson("/api/correct", { turn_id: turn.turn_id, session_id: turn.session_id, corrected_text: textarea.value });
  text("evaluation-status", result.ok ? "Đã lưu correction" : result.reason, "");
  if (result.ok) loadReviewQueue();
}

async function loadReviewQueue() {
  const host = byId("evaluation-turns");
  setListEmpty(host, "Đang tải…");
  try {
    const response = await fetch("/api/recent_turns?n=20");
    const data = await response.json();
    clear(host);
    host.classList.remove("empty");
    (data.turns || []).forEach((turn) => {
      const card = div("review-card");
      card.appendChild(div("meta", `${turn.session_id || "session"} · #${turn.turn_id} · ${turn.kind || "turn"}`));
      if (turn.user_text) card.appendChild(div("muted", `Viewer: ${turn.user_text}`));
      card.appendChild(div("", turn.mai_text || turn.parsed_text || ""));
      const textarea = document.createElement("textarea");
      textarea.value = turn.mai_text || turn.parsed_text || "";
      textarea.setAttribute("aria-label", `Correction turn ${turn.turn_id}`);
      card.appendChild(textarea);
      const buttons = div("button-row");
      [["Good", "good"], ["Bad", "bad"], ["Flag", "flag"]].forEach(([label, rating]) => {
        const button = document.createElement("button");
        button.className = "button compact";
        button.textContent = label;
        button.addEventListener("click", () => rateTurn(rating, turn));
        buttons.appendChild(button);
      });
      const save = document.createElement("button");
      save.className = "button primary compact";
      save.textContent = "Lưu correction";
      save.addEventListener("click", () => correctTurn(turn, textarea));
      buttons.appendChild(save);
      card.appendChild(buttons);
      host.appendChild(card);
    });
    if (!host.children.length) setListEmpty(host, "Review queue trống.");
  } catch (error) {
    setListEmpty(host, `Không tải được review queue: ${error}`);
  }
}

function render(snapshot) {
  ui.snapshot = snapshot;
  renderOverview(snapshot);
  renderBrain(snapshot);
  renderConversation(snapshot);
  renderSystem(snapshot);
}

function setSourceMode(mode) {
  if (!["auto", "live", "history"].includes(mode)) return;
  ui.sourceMode = mode;
  document.querySelectorAll("[data-source]").forEach((button) => button.classList.toggle("active", button.dataset.source === mode));
  if (mode === "history") {
    switchSection("conversation");
    loadHistory();
  }
  connect();
}

document.querySelectorAll("[data-source]").forEach((button) => button.addEventListener("click", () => setSourceMode(button.dataset.source)));
byId("operator-pause").addEventListener("click", () => postJson("/api/agent/pause", { reason: "operator dashboard pause" }));
byId("operator-resume").addEventListener("click", () => postJson("/api/agent/resume", { reason: "operator dashboard resume" }));
byId("operator-goal-pin").addEventListener("click", () => postJson("/api/goals/pin", { reason: byId("operator-goal-reason").value, success_condition: byId("operator-goal-success").value }));
byId("evaluation-refresh").addEventListener("click", loadReviewQueue);
document.querySelectorAll("[data-rating]").forEach((button) => button.addEventListener("click", () => rateTurn(button.dataset.rating)));
byId("history-filters").addEventListener("submit", (event) => { event.preventDefault(); loadHistory(); });
byId("history-reset").addEventListener("click", () => { byId("history-filters").reset(); loadHistory(); });

const emergencyDialog = byId("emergency-dialog");
byId("operator-emergency").addEventListener("click", () => emergencyDialog.showModal());
byId("emergency-confirm").addEventListener("click", async (event) => {
  event.preventDefault();
  const result = await postJson("/api/emergency_stop", {});
  emergencyDialog.close(result.ok ? "confirmed" : "failed");
});

setInterval(() => text("operator-clock", new Date().toLocaleTimeString("vi-VN")), 1000);

function connect() {
  ui.connectGeneration += 1;
  const generation = ui.connectGeneration;
  if (ui.reconnectTimer) clearTimeout(ui.reconnectTimer);
  if (ui.socket) {
    ui.socket.onclose = null;
    ui.socket.close();
  }
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws?source=${encodeURIComponent(ui.sourceMode)}`);
  ui.socket = socket;
  socket.onopen = () => {
    if (generation !== ui.connectGeneration) return;
    ui.wsConnected = true;
    renderConnection(ui.snapshot || {});
  };
  socket.onmessage = (event) => {
    if (generation !== ui.connectGeneration) return;
    try { render(JSON.parse(event.data)); } catch (_) { /* keep the last valid snapshot */ }
  };
  socket.onclose = () => {
    if (generation !== ui.connectGeneration) return;
    ui.wsConnected = false;
    renderConnection(ui.snapshot || {});
    ui.reconnectTimer = setTimeout(connect, 2000);
  };
}

buildRadar(byId("mood-radar-compact"));
buildRadar(byId("mood-radar-detail"));
connect();
