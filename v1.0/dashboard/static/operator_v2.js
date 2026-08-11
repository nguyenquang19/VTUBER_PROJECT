"use strict";

const ui = { snapshot: null, section: "overview", wsConnected: false };
const sectionTitles = { overview: "Tổng quan", brain: "Brain", conversation: "Hội thoại", system: "Hệ thống", evaluation: "Đánh giá" };
const byId = (id) => document.getElementById(id);
const text = (id, value, fallback = "—") => { const node = byId(id); if (node) node.textContent = value == null || value === "" ? fallback : String(value); };
const clear = (node) => { while (node && node.firstChild) node.removeChild(node.firstChild); };
const div = (className, value) => { const node = document.createElement("div"); node.className = className; if (value != null) node.textContent = String(value); return node; };

async function postJson(url, body = {}) {
  try {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    return { status: response.status, ...(await response.json()) };
  } catch (error) { return { ok: false, reason: String(error) }; }
}

function switchSection(name) {
  ui.section = name;
  document.querySelectorAll(".operator-nav button").forEach((button) => button.classList.toggle("active", button.dataset.section === name));
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `section-${name}`));
  text("page-title", sectionTitles[name]);
  if (name === "evaluation") loadReviewQueue();
}
document.querySelectorAll(".operator-nav button").forEach((button) => button.addEventListener("click", () => switchSection(button.dataset.section)));

function setListEmpty(node, message) { clear(node); node.classList.add("empty"); node.textContent = message; }
function addStackItem(node, title, detail, tone = "") {
  node.classList.remove("empty");
  const row = div(`stack-item ${tone}`.trim());
  row.appendChild(div("", title)); row.firstChild.tagName && (row.firstChild.style.fontWeight = "650");
  if (detail) row.appendChild(div("muted", detail));
  node.appendChild(row);
}
function addChip(node, value) { node.appendChild(div("chip", value)); }
function addThreadItem(node, thread) {
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
  addChip(meta, `${(thread.viewer_contributions || []).length} viewer inputs`);
  row.appendChild(meta);
  if ((thread.open_questions || []).length) {
    row.appendChild(div("thread-question", `Đang chờ: ${thread.open_questions.at(-1).text}`));
  }
  node.appendChild(row);
}

function renderConnection(snapshot) {
  const node = byId("connection-status");
  const online = Boolean((snapshot.runtime || {}).online);
  if (!ui.wsConnected) { node.textContent = "Mất kết nối"; node.className = "pill critical"; return; }
  node.textContent = online ? "Runtime online" : "Snapshot offline";
  node.className = `pill ${online ? "ready" : "warning"}`;
}

function renderOverview(snapshot) {
  const overview = snapshot.operator_overview || {};
  const hero = byId("status-hero");
  hero.className = `status-hero ${overview.overall_status || "neutral"}`;
  text("overview-headline", overview.headline, "Đang chờ snapshot…");
  text("overview-action", overview.action_required, "Chưa có yêu cầu vận hành.");
  text("overview-runtime", overview.runtime_online ? "Online" : "Offline");
  text("overview-incidents", overview.unresolved_incidents || 0);
  text("overview-current-action", overview.current_action);
  text("overview-delivery", overview.current_delivery_state);
  renderConnection(snapshot);
  byId("operator-emergency").disabled = !overview.controls_available;

  const recovery = byId("overview-recovery");
  recovery.hidden = !overview.recovery_action;
  recovery.textContent = overview.recovery_action === "resume_agent" ? "Resume agent" : overview.recovery_action === "resume_emergency" ? "Resume output" : "Mở chi tiết";
  recovery.onclick = async () => {
    if (overview.recovery_action === "resume_agent") await postJson("/api/agent/resume", { reason: "operator dashboard recovery" });
    else if (overview.recovery_action === "resume_emergency") await postJson("/api/resume", {});
    else switchSection(overview.recovery_action === "inspect_decision" ? "brain" : "system");
  };

  renderDecision(snapshot.decisions);
  renderGoal(overview.active_goal, overview.controls_available);
  const health = byId("overview-health");
  setListEmpty(health, "Không có service cần chú ý.");
  (overview.unhealthy_services || []).forEach((service) => { if (health.classList.contains("empty")) clear(health); addStackItem(health, service, "Cần kiểm tra health/recovery", "failed"); });
  const incidents = byId("overview-incident-list");
  setListEmpty(incidents, "Không có incident.");
  const recent = ((snapshot.incidents || {}).recent || []).slice().reverse();
  recent.forEach((item) => { if (incidents.classList.contains("empty")) clear(incidents); addStackItem(incidents, `${item.severity || "warning"} · ${item.component || "unknown"}`, item.action || item.summary || item.status); });
}

function renderDecision(decisions) {
  const current = (decisions || {}).current || {};
  text("decision-action", current.action);
  text("decision-reason", current.reason);
  text("decision-id", current.decision_id);
  text("decision-transaction", current.transaction_id || current.transaction_state);
  const outcome = byId("decision-outcome"); outcome.textContent = current.outcome || "—"; outcome.className = `pill ${current.delivery_state === "failed" ? "critical" : current.outcome === "committed" || current.outcome === "completed" ? "ready" : "neutral"}`;
  const evidence = byId("decision-evidence"); clear(evidence);
  (current.evidence_refs || []).forEach((value) => addChip(evidence, value));
  if (!evidence.children.length) evidence.appendChild(div("muted", "Chưa có evidence."));
}

function renderGoal(goal, controls) {
  const host = byId("overview-goal"); clear(host);
  if (!goal) { host.className = "empty"; host.textContent = "Chưa có goal active."; }
  else {
    host.className = "stack-list"; addStackItem(host, `${goal.kind || "goal"} · P${goal.priority || 0}`, goal.reason || goal.id || goal.goal_id);
    const actions = div("button-row");
    [["Complete", "complete"], ["Cancel", "cancel"]].forEach(([label, action]) => { const button = document.createElement("button"); button.className = action === "cancel" ? "button danger-soft" : "button"; button.textContent = label; button.disabled = !controls; button.addEventListener("click", () => postJson(`/api/goals/${encodeURIComponent(goal.id || goal.goal_id)}/${action}`, { reason: "operator dashboard" })); actions.appendChild(button); });
    host.appendChild(actions);
  }
  byId("operator-goal-pin").disabled = !controls;
}

function renderBrain(snapshot) {
  const decisions = snapshot.decisions || { recent: [] };
  const list = byId("brain-decisions"); setListEmpty(list, "Chưa có quyết định.");
  (decisions.recent || []).forEach((record) => {
    if (list.classList.contains("empty")) clear(list);
    list.classList.remove("empty");
    const row = div(`decision-item ${record.delivery_state === "failed" ? "failed" : record.outcome === "committed" || record.outcome === "completed" ? "committed" : ""}`);
    const title = document.createElement("strong"); title.textContent = `${record.action || "wait"} · ${record.reason || "unknown"}`; row.appendChild(title);
    row.appendChild(div("muted mono", `${record.decision_id || ""} · ${record.transaction_state || record.delivery_state || "not_started"}`));
    const chips = div("chip-row"); (record.evidence_refs || []).forEach((value) => addChip(chips, value)); if (chips.children.length) row.appendChild(chips);
    list.appendChild(row);
  });
  text("brain-record-count", (decisions.recent || []).length);
  const summary = ((decisions.current || {}).candidate_summary) || {};
  const candidateHost = byId("brain-candidates"); clear(candidateHost);
  [["Candidates", summary.candidate_count], ["Pool size", summary.pool_size], ["Pulse", summary.pulse_state], ["Top score", summary.top_score], ["Active goal", summary.active_goal_id], ["Safety hold", summary.safety_hold ? "yes" : "no"]].forEach(([key, value]) => { const cell = document.createElement("div"); const dt = document.createElement("dt"); dt.textContent = key; const dd = document.createElement("dd"); dd.textContent = value == null || value === "" ? "—" : String(value); cell.append(dt, dd); candidateHost.appendChild(cell); });
  const thought = snapshot.thought_engine || {};
  text("thought-stage", thought.stage || "idle");
  byId("thought-stage").className = `pill ${thought.pending_interrupted ? "warning" : thought.stage ? "ready" : "neutral"}`;
  fillDl("thought-engine", [["Cause", thought.cause], ["Intention", thought.intention], ["Pending", thought.pending_plan_id], ["Ledger", (thought.ledger || []).length]]);

  const moodSnapshot = snapshot.mood || {};
  const mood = moodSnapshot.mood_pos || moodSnapshot.current_mood || {};
  const target = moodSnapshot.mood_target || {};
  const moodHost = byId("brain-mood"); clear(moodHost); moodHost.className = "mood-columns";
  const moodNames = { vui: "Vui", buon: "Buồn", buc: "Bực", bon_chon: "Bồn chồn", nguong: "Ngượng" };
  const moodColors = { vui: "#78e0b4", buon: "#72a7ff", buc: "#ff766f", bon_chon: "#ffc568", nguong: "#d99cff" };
  Object.keys(moodNames).forEach((name) => {
    if (mood[name] == null) return;
    const value = Math.max(0, Math.min(10, Number(mood[name])));
    const targetValue = Math.max(0, Math.min(10, Number(target[name] == null ? value : target[name])));
    const column = div("mood-column");
    column.style.setProperty("--mood-color", moodColors[name]);
    column.appendChild(div("mood-value mono", value.toFixed(2)));
    const well = div("mood-well");
    const fill = div("mood-column-fill"); fill.style.height = `${value * 10}%`;
    const marker = div("mood-target"); marker.style.bottom = `calc(${targetValue * 10}% - 1px)`; marker.title = `Target ${targetValue.toFixed(2)}`;
    well.append(fill, marker); column.append(well, div("mood-name", moodNames[name])); moodHost.appendChild(column);
  });
  if (!moodHost.children.length) setListEmpty(moodHost, "Chưa có mood snapshot.");
  const sampleTime = moodSnapshot.sampled_at ? new Date(moodSnapshot.sampled_at).toLocaleTimeString("vi-VN") : null;
  text("brain-mood-meta", sampleTime ? `${sampleTime} · tick ${moodSnapshot.ticks == null ? "—" : moodSnapshot.ticks}` : "snapshot tĩnh");
}

function renderConversation(snapshot) {
  const agent = snapshot.agent || {};
  text("conversation-phase", agent.stream_phase);
  text("conversation-topic", (agent.current_topic || {}).topic || (agent.current_topic || {}).summary);
  text("conversation-last-speech", agent.last_spoken_summary);
  const threads = byId("conversation-threads"); setListEmpty(threads, "Không có thread mở.");
  (agent.open_threads || []).forEach((thread) => { if (threads.classList.contains("empty")) clear(threads); addThreadItem(threads, thread); });
  const actions = byId("conversation-actions"); setListEmpty(actions, "Queue trống.");
  (((snapshot.operations || {}).action_queue) || []).forEach((item) => { if (actions.classList.contains("empty")) clear(actions); addStackItem(actions, item.kind || "action", item.id || item.status || `${item.pending_count || 0} pending`); });
  text("conversation-environment", agent.environment_summary ? JSON.stringify(agent.environment_summary, null, 2) : "Chưa có environment.");
  const relationships = byId("conversation-relationships"); setListEmpty(relationships, "Chưa có profile.");
  (((snapshot.relationships || {}).profiles) || []).forEach((profile) => { if (relationships.classList.contains("empty")) clear(relationships); addStackItem(relationships, profile.viewer_id || "pseudonym", `${profile.interaction_count || 0} interactions · ${profile.tone || "no tone"}`); });
}

function addMetric(host, label, value) { const card = document.createElement("article"); card.append(div("", label), div("", value == null ? "—" : value)); card.firstChild.style.color = "var(--muted)"; card.lastChild.style.fontSize = "19px"; card.lastChild.style.fontWeight = "700"; host.appendChild(card); }
function fillDl(id, pairs) { const host = byId(id); clear(host); pairs.forEach(([key, value]) => { const cell = document.createElement("div"); const dt = document.createElement("dt"); dt.textContent = key; const dd = document.createElement("dd"); dd.textContent = value == null ? "—" : String(value); cell.append(dt, dd); host.appendChild(cell); }); }

function renderSystem(snapshot) {
  const metrics = snapshot.metrics || {}, llm = snapshot.llm || {}, tts = snapshot.tts || {}, filter = snapshot.filter || {};
  const metricHost = byId("system-metrics"); clear(metricHost); const gpuSuffix = metrics.gpu_metrics_stale ? " (stale)" : ""; addMetric(metricHost, "GPU", `${metrics.gpu_util_percent == null ? "—" : metrics.gpu_util_percent + "%"}${gpuSuffix}`); addMetric(metricHost, "VRAM", `${metrics.vram_mb == null ? "—" : metrics.vram_mb + " / " + (metrics.vram_total_mb == null ? "—" : metrics.vram_total_mb) + " MB"}${gpuSuffix}`); addMetric(metricHost, "LLM TTFT", `${llm.last_ttft_ms == null ? "—" : llm.last_ttft_ms} ms`); addMetric(metricHost, "TTS TTFA", `${tts.last_ttfa_ms == null ? "—" : tts.last_ttfa_ms} ms`);
  const healthHost = byId("system-health"); setListEmpty(healthHost, "Chưa có health snapshot.");
  Object.entries(((snapshot.health_supervisor || {}).targets) || {}).forEach(([name, value]) => { if (healthHost.classList.contains("empty")) clear(healthHost); addStackItem(healthHost, `${name} · ${value.health || "unknown"}`, value.message || value.last_action || "", value.circuit_open ? "failed" : ""); });
  const featureHost = byId("system-features"); setListEmpty(featureHost, "Chưa có feature snapshot.");
  (snapshot.features || []).forEach((feature) => { if (featureHost.classList.contains("empty")) clear(featureHost); const row = div("stack-item"); const label = document.createElement("strong"); label.textContent = `${feature.id} · ${feature.status}`; row.appendChild(label); if (!feature.is_core) { const button = document.createElement("button"); button.className = "button"; button.textContent = feature.enabled ? "Disable" : "Enable"; button.addEventListener("click", () => postJson(`/api/features/${encodeURIComponent(feature.id)}/toggle`, {})); row.appendChild(button); } featureHost.appendChild(row); });
  fillDl("system-llm", [["Requests", llm.requests_total], ["Parse ok", `${llm.parse_rate_percent == null ? "—" : llm.parse_rate_percent}%`], ["Fallback", llm.fallback_total], ["Filter hits", filter.hits_total]]);
  fillDl("system-tts", [["Turns", tts.turns_total], ["Subtitle fallback", tts.subtitle_fallback_total], ["Played chunks", (tts.player || {}).chunks_played], ["Dropped chunks", (tts.player || {}).chunks_dropped]]);
  const runtime = snapshot.runtime || {}, operations = snapshot.operations || {};
  byId("operator-pause").disabled = !runtime.controls_available || operations.paused;
  byId("operator-resume").disabled = !runtime.controls_available || !operations.paused;
}

async function rateTurn(rating, turn = null) {
  const payload = turn ? { rating, turn_id: turn.turn_id, session_id: turn.session_id } : { rating };
  const result = await postJson("/api/rate", payload); text("evaluation-status", result.ok ? `Đã lưu ${rating}` : result.reason, ""); if (result.ok && turn) loadReviewQueue();
}
async function correctTurn(turn, textarea) { const result = await postJson("/api/correct", { turn_id: turn.turn_id, session_id: turn.session_id, corrected_text: textarea.value }); text("evaluation-status", result.ok ? "Đã lưu correction" : result.reason, ""); if (result.ok) loadReviewQueue(); }
async function loadReviewQueue() {
  const host = byId("evaluation-turns"); setListEmpty(host, "Đang tải…");
  try {
    const response = await fetch("/api/recent_turns?n=20"); const data = await response.json(); clear(host); host.classList.remove("empty");
    (data.turns || []).forEach((turn) => { const card = div("review-card"); card.appendChild(div("meta", `${turn.session_id || "session"} · #${turn.turn_id} · ${turn.kind || "turn"}`)); if (turn.user_text) card.appendChild(div("muted", `User: ${turn.user_text}`)); card.appendChild(div("", turn.mai_text || turn.parsed_text || "")); const textarea = document.createElement("textarea"); textarea.value = turn.mai_text || turn.parsed_text || ""; textarea.setAttribute("aria-label", `Correction turn ${turn.turn_id}`); card.appendChild(textarea); const buttons = div("button-row"); [["Good", "good"], ["Bad", "bad"], ["Flag", "flag"]].forEach(([label, rating]) => { const button = document.createElement("button"); button.className = "button"; button.textContent = label; button.addEventListener("click", () => rateTurn(rating, turn)); buttons.appendChild(button); }); const save = document.createElement("button"); save.className = "button primary"; save.textContent = "Lưu correction"; save.addEventListener("click", () => correctTurn(turn, textarea)); buttons.appendChild(save); card.appendChild(buttons); host.appendChild(card); });
    if (!host.children.length) setListEmpty(host, "Review queue trống.");
  } catch (error) { setListEmpty(host, `Không tải được review queue: ${error}`); }
}

function render(snapshot) { ui.snapshot = snapshot; renderOverview(snapshot); renderBrain(snapshot); renderConversation(snapshot); renderSystem(snapshot); }

byId("operator-emergency").addEventListener("click", () => postJson("/api/emergency_stop", {}));
byId("operator-pause").addEventListener("click", () => postJson("/api/agent/pause", { reason: "operator dashboard pause" }));
byId("operator-resume").addEventListener("click", () => postJson("/api/agent/resume", { reason: "operator dashboard resume" }));
byId("operator-goal-pin").addEventListener("click", () => postJson("/api/goals/pin", { reason: byId("operator-goal-reason").value, success_condition: byId("operator-goal-success").value }));
byId("evaluation-refresh").addEventListener("click", loadReviewQueue);
document.querySelectorAll("[data-rating]").forEach((button) => button.addEventListener("click", () => rateTurn(button.dataset.rating)));
setInterval(() => text("operator-clock", new Date().toLocaleTimeString("vi-VN")), 1000);

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws`);
  socket.onopen = () => { ui.wsConnected = true; if (ui.snapshot) renderConnection(ui.snapshot); else text("connection-status", "WS connected"); };
  socket.onmessage = (event) => { try { render(JSON.parse(event.data)); } catch (_) { /* keep last valid view */ } };
  socket.onclose = () => { ui.wsConnected = false; renderConnection(ui.snapshot || {}); setTimeout(connect, 2000); };
}
connect();
