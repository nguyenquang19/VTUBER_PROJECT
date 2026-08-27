# Mai V2 — Đặc tả hệ thống

> **Vai trò:** nguồn sự thật duy nhất về **hành vi đang chạy hiện tại**. Viết từ code working tree `v2.0/`,
> không phải kế hoạch tương lai.
>
> **Product version:** lấy duy nhất từ `config/system.yaml::app.version` (hiện `1.4.3`).
>
> **Cho tương lai/scope:** xem `docs/ROADMAP.md`. **Cho lịch sử đóng băng:** xem `docs/V1_BASELINE.md`.

## Thứ tự nguồn sự thật

Khi tài liệu và code mâu thuẫn, tin theo thứ tự: `interfaces/` → `orchestrator/stream_runtime.py`
(composition root) → `services/` → `config/*.yaml` → `tests/` → tài liệu này → README/roadmap.
Data contract chi tiết **không chép vào đây** — `interfaces/` là owner. Tài liệu này mô tả cấu trúc,
luồng, invariant và trạng thái; không nhân bản định nghĩa kiểu.

---

## 1. Mai là gì

AI VTuber tiếng Việt chạy **local trên Windows 11**. Backend hội thoại là `llama.cpp`
(`llama-server.exe`), đầu vào là chat YouTube/Discord, đầu ra là text + audio VieNeu-TTS + subtitle
fallback + avatar VTube Studio. Runtime có Director quyết định hành động, mood Hybrid, working memory
luôn có và semantic memory bật mặc định sau feature flag, transaction tại ranh giới delivery, dashboard
operator và bộ công cụ đánh giá offline.

Product `1.4.3` là đường hội thoại đã phát hành. Các năng lực V2 (world/self model, Brain, external
action) tồn tại ở nhiều mức: có mã / đã ghép / đã test / đã phát hành — xem Mục 8.

---

## 2. Mô hình cốt lõi

Ba nguyên tắc chi phối toàn hệ thống. Hiểu sai ba cái này thì đọc code sẽ thấy mâu thuẫn.

1. **Tick-driven, không reactive.** Hệ thống không "nhận tin → trả lời". Mỗi `kernel.tick_seconds`
   (mặc định **1.5s**) một nhịp thức dậy và hỏi "lúc này nên làm gì?". Tin nhắn chỉ đổ vào hồ chờ;
   nhịp tick mới quyết định.
2. **Quyết định ≠ sinh chữ.** Director chỉ chọn **một hành động** và trỏ vào tin nào. LLM viết câu ở
   bước sau, dựa trên quyết định đó.
3. **Tạo câu ≠ đã nói.** Trạng thái chỉ ghi "đã nói" khi delivery được xác nhận. Thứ tự bất biến:
   `verify → commit → project`.

Hai đồng hồ chạy song song, độc lập:

| Đồng hồ | Nhịp | Owner | Việc |
|---|---|---|---|
| Quyết định | 1.5s | `TurnKernel → DirectorLoop` | đọc hồ + trạng thái → chọn 1 action → (nếu cần) sinh chữ + phát |
| Cảm xúc | 10Hz | `EmotionOrchestrator` | cập nhật mood (spring/decay) kể cả khi không có event |

---

## 3. Kiến trúc — 13 lớp

Xếp từ lớp nền (composition) xuống các lớp chức năng. Bốn thư mục `services/action`, `world`,
`self_model`, `qc` hiện **rỗng** (placeholder).

| # | Lớp | Thư mục | Trách nhiệm |
|---|---|---|---|
| L0 | Composition & vòng đời | `orchestrator/` | đọc config, tạo + wire + lifecycle mọi service |
| L1 | Nguồn vào | `services/input/` | nhận event thô, chuẩn hóa, đóng dấu nguồn |
| L2 | Ingress & perception | `services/ingress · perception/` | admission chuẩn hóa, dedup, bounded, không quyết định |
| L3 | Trạng thái quyền lực | `services/state/` | một reducer canonical; nguồn + độ tin + TTL + quyền ghi |
| L4 | Nhận thức & ngữ cảnh | `services/cognition/` | dựng context; Cognitive Brain (proposal-only) |
| L5 | Mục tiêu · thread · ký ức · quan hệ | `services/agent · memory · relationship/` | chất liệu grounded để có gì mà nói |
| L6 | Cảm xúc / mood | `services/emotion · orchestrator/` | 10Hz, 2 kênh (số deterministic + sắc thái LLM) |
| L7 | Điều phối / quyết định | `services/director · kernel/` | nhịp 1.5s, chọn 1 action mỗi tick |
| L8 | Sinh ngôn ngữ | `services/llm · filter/` | prompt → LLM → parse → filter, có fallback chain |
| L9 | Thực thi & giao dịch | `services/execution/` | reserve → execute → verify → commit → project |
| L10 | Đầu ra: giọng & nhân vật | `services/tts · animation/` | TTS chain, audio player, VTS avatar, embodiment |
| L11 | Vận hành & quan sát | `services/operations · dashboard/` | metric, journal, operator control, emergency, shutdown |
| L12 | Đánh giá & cổng phát hành | `services/evaluation/` | offline: chấm chất lượng, release gate, canary |

### L0 — Composition & vòng đời (`orchestrator/`)

Chỗ ghép toàn hệ thống. Không business logic, chỉ lắp ráp.

| Module | Vai trò |
|---|---|
| `stream_runtime.py` | Composition root (~2.8k dòng): tạo + wire + lifecycle mọi service. |
| `features.py` | `FeatureManager`: 50 cờ, dependency/conflict/budget fail-closed, persist atomic. |
| `config_loader.py` · `runtime_config_validation.py` | Nạp + validate 31 YAML, fail-closed với giá trị sai. |
| `logger.py` · `migration_runner.py` | Structured logging; migration schema khi khởi động. |

Khởi động: đọc config → tạo service theo thứ tự phụ thuộc → mở tài nguyên; lỗi giữa chừng rollback ngược
thứ tự, luôn `stop()` trong `finally`.

### L1 — Nguồn vào (`services/input/`)

`youtube_chat` (pytchat, no OAuth) · `discord_chat` (discord.py, cần `DISCORD_BOT_TOKEN`) ·
`youtube_replay` (offline `*.live_chat.json`, replay tất định) · `chat_router` (glue N nguồn →
EmotionOrchestrator + LLMTurnRunner, intake mode, dedup). Scraper chạy async liên tục, đổ vào hồ; độc
lập với nhịp tick.

### L2 — Ingress & perception (`services/ingress · perception/`)

`ingress/normalizer` (legacy → canonical shape) · `ingress/adapters` (route legacy writes qua canonical
ingress) · `perception/ingress` (strict bounded, **no decision ownership**) · `perception/adapters`
(Chat/System/OBS read-only — OBS mặc định tắt).

### L3 — Trạng thái quyền lực (`services/state/`)

Lớp quan trọng nhất của V2: lưu cả **nguồn, độ tin cậy, TTL, quyền ghi đè**, không chỉ giá trị.

| Module | Vai trò | Trạng thái |
|---|---|---|
| `authoritative.py` | Ranh giới mutation canonical duy nhất trên các domain reducer. | LIVE |
| `agent.py` | Reducer + AgentState (đang nói/chờ, ý định). | LIVE |
| `world.py` | World Model reducer ("bên ngoài đang xảy ra gì"). | SHADOW |
| `self_projection.py` | SelfSnapshot read-only. | SHADOW |
| `continuity.py` | Owner post-commit cho turn đã giao thành công. | LIVE |
| `event_ledger.py` | Sổ sự kiện grounded bounded in-memory. | LIVE |

Agent/World/Perception mutation → `AuthoritativeStateReducer` → đọc qua `AuthoritativeStateSnapshot`.

### L4 — Nhận thức & ngữ cảnh (`services/cognition/`)

| Module | Vai trò |
|---|---|
| `context_builder.py` | Chiếu read-only state → Cognitive Context + Focus (~1.2k dòng). |
| `brain.py` | Cognitive Brain **proposal-only**: 1 call LLM → JSON strict `{mode, speech_text, evidence_refs, uncertainty, reason_codes, attention_target_id, intent}`. "Không tự execute proposal." **SHADOW.** |
| `model_adapter.py` | Ranh giới prompt/model duy nhất cho Brain. |
| `scheduler.py` | Latest-wins, single-inflight; preempt khi có input live. |
| `agent_context_projection · compatibility_context` | Chiếu context/text exact cho đường DirectorLoop. |

### L5 — Mục tiêu · thread · ký ức · quan hệ

`agent/` (deterministic, grounded): `goal_manager` (state machine 1-goal-active) · `goal_proposal`
(LLM đề xuất, GoalManager vẫn authority — OFF) · `thread_detector · open_thread_manager` (thread
grounded: câu hỏi/promise/story) · `behavior_library · repair_policy · conversation_move_planner ·
session_recap · mood_policy · topic_matcher`.

`memory/`: `semantic_memory` (bge-m3 1024-dim CPU + sqlite-vec, hard bound 150ms — **LIVE mặc định**) ·
`working_memory` (deque bounded, luôn là fallback) · `memory_fallback` (semantic → working, semantic lỗi
khởi động/query thì degrade working-only) · `episodic` (rolling session summary bằng llama.cpp ở workload
shadow, **LIVE mặc định** sau `episodic_memory`) · `embedder · sqlite_vec_store · extractor`. Entry tự động
chỉ được tạo sau verified delivery và lưu **bí danh + ý nghĩa đã sanitize**, không lưu transcript, tên thật,
ngày sinh hoặc định danh trực tiếp. Episodic chỉ giữ turn nguồn trong RAM bounded tới lần summary, lưu summary
bounded cùng provenance outcome, session scope và expiry; query loại entry sai session/hết hạn rồi rerank
candidate episodic theo recency + salience. Tắt `episodic_memory` giữ nguyên chain A1; tắt `memory_semantic`
đưa runtime về working-only mà không làm chết turn. Metric owner của chain gồm semantic hit/miss, query
latency p95 trên cửa sổ bounded, timeout/fallback và episodic observed/generated/rejected/failed/evicted/
expired/retrieved/pending.

`relationship/`: `manager` (hồ sơ pseudonymous M7 — LIVE) · `store` (SQLite chỉ bí danh, **no PII**).

### L6 — Cảm xúc / mood (`services/emotion · orchestrator/`)

Subsystem **10Hz** song song, **2 kênh tách bạch**:

- **Kênh A — điểm số (deterministic, KHÔNG LLM):** `classifier` (event → 1/24 category) → `appraisal`
  (tra bảng `emotion_appraisal.yaml`, vd `donation_large:{vui:9,nguong:6}`) → `modifiers` (nhân/cộng
  bounded) → `MoodEngine` (spring/decay 5 chiều `vui/buon/buc/bon_chon/nguong`).
- **Kênh B — sắc thái (LLM):** các category `{}` để LLM lo; `mood_style` đổi số → chỉ dẫn giọng bằng CHỮ.

`hybrid_affect` ghép Mood v1 tone + Mood v2 policy (Hybrid — LIVE). Mood feed vào: quyết định
(`mood_policy.proactive_ready`), prompt (directive), và nhận feedback từ output LLM.

### L7 — Điều phối / quyết định (`services/director · kernel/`)

Trái tim nhịp 1.5s. Chọn **một** action mỗi tick.

| Module | Vai trò | Trạng thái |
|---|---|---|
| `salience.py` | Pool chấm điểm + decay + cluster chat (thay FIFO). | LIVE |
| `chat_pulse.py` | Đo độ sôi nổi chat → tín hiệu Director + mood + urge. | LIVE |
| `director.py` | Decision engine rule-based; cũng giữ hard-preempt an toàn/segment. | LIVE |
| `director_loop.py` | Turn driver duy nhất (~2.5k dòng): tick → decide → execute → mark_spoke. | LIVE |
| `v2_shadow · v2_takeover · v2_primary` | V2 propose read-only · selector ownership · materialize. | LIVE (primary) |
| `kernel/turn_kernel.py` | Single tick owner S4; route public → DirectorLoop; offer Brain shadow. | LIVE |
| `proactive_policy · speech_style · trajectory · decision_record` | Ứng viên tự nói; guard phong cách; ghi hành trình replay. | LIVE |

Luồng quyết định chi tiết ở Mục 4.

### L8 — Sinh ngôn ngữ (`services/llm · filter/`)

`llama_cpp_llm` (stream qua llama-server — backend đã chốt) · `llm_turn` (1 lượt qua fallback chain) ·
`prompt_manager · prompt_cache` (cache prefix persona) · `parser` · `process_manager` (1 instance,
port 8080) · `canned_response` (tầng cuối khi LLM chết). `filter/rule_filter` (regex persona/explicit) +
`regenerator` (re-prompt khi persona_break).

### L9 — Thực thi & giao dịch (`services/execution/`)

Bảo đảm "tạo ≠ đã làm".

| Module | Vai trò |
|---|---|
| `transaction.py` | State machine giao dịch bounded (reserve/deliver/commit/release). |
| `coordinator.py` | Typed local execution coordinator (S5). |
| `outcome.py` | Owner commit terminal + verified-outcome duy nhất (S5). |
| `speech.py · local.py` | Delivery boundary; adapter strict quanh speech + VTS (LIVE test). |
| `obs.py · external.py · registry.py` | OBS scene transport/executor/verifier + transaction (OFF). |
| `mock.py · mock_backend.py` | Vòng mock chứng minh giao dịch action chung (Phase 5). |

Bất biến: `reserve` (idempotency key, chống nói 2 lần) → execute → **verify → commit → project**. Lỗi
trước commit release transaction, giữ World nguyên; lỗi projection sau commit ghi riêng, không đổi
transaction đã commit.

### L10 — Đầu ra: giọng & nhân vật (`services/tts · animation/`)

`tts_pipeline` (text → split → chain → player) · `vieneu_service` (VieNeu-TTS v3 Turbo, TTFA ~308ms,
VRAM 0.37GB) · `sentence_splitter` (VN) · `audio_player` (tuần tự, no-overlap) · `pitch · pacing ·
natural_timing` (filler gated TTFA, không che latency chat) · `subtitle_fallback` (tầng cuối). `animation/
vts_service · vts_transport · embodiment_policy` (LOW/MID/HIGH arbitration — LIVE, fail-safe khi VTS thiếu).

### L11 — Vận hành & quan sát (`services/operations · dashboard/`)

`metrics` (Prometheus) · `surface` (snapshot live + lệnh allowlisted) · `control_plane` (pause/resume
audited) · `emergency_control` (latch fail-closed) · `health_supervisor` (backoff + circuit breaker) ·
`incident_log · turn_journal` (append-only privacy-safe) · `shutdown_coordinator` (tắt có thứ tự +
snapshot) · `dashboard_data_source · standalone_snapshot · post_stream_review`. Dashboard loopback
`:7860`, token-gated (`MAI_DASHBOARD_CONTROL_TOKEN`).

### L12 — Đánh giá & cổng phát hành (`services/evaluation/`, offline)

`harness` (so scenario có evidence) · `human_like` (MAI-HLC blind review persist-before-reveal) ·
`quality_judge` (LLM-judge lọc ngữ nghĩa) · `release_gate` (Phase 15 strict source/hash/freshness) ·
`closed_loop_canary` (operator-only — OFF) · `data_quality · acceptance · simulator · soak · readiness ·
mood_ab · review · scenario_loader`. Không đụng runtime quyết định.

---

## 4. Luồng runtime — quyết định câu nói

### 4.1. Chuỗi gọi mỗi tick

`stream_runtime` wire: `TurnKernel(compatibility=director_loop, rollout_mode=shadow)` +
`director_loop.configure_director_v2_takeover(shadow, takeover)`.

```
TurnKernel._loop (ngủ 1.5s)
  └─ director_loop.tick_once()          # kernel gọi thẳng DirectorLoop
       ├─ evict_stale + update pulse
       ├─ dựng DirectorInput (chat + goals + agent_state + segment + dead-air + mood)
       ├─ _select_director_decision()   # ← CHỐT câu public (4.2)
       └─ nếu action ≠ WAIT: turn_lock → transaction (4.3)
  └─ (chỉ khi rollout_mode=shadow) brain_scheduler.offer(...)   # Brain nhận cơ hội SAU execute
```

> **Bẫy naming — đọc kỹ.** Trong `turn_kernel`, `public_owner` **hardcode = COMPATIBILITY**. "COMPATIBILITY"
> ở đây nghĩa là **"đường DirectorLoop, không phải Brain"** — KHÔNG phải legacy Compatibility Director.
> Metric/health in `public_owner=compatibility` là do vậy; đừng đọc nhầm thành "legacy đang quyết".

### 4.2. Thuật toán chốt quyết định (`_select_director_decision`)

Config tracked: `director.yaml::director_v2_takeover.ownership_mode = primary`, feature
`director_v2_takeover.enabled = true` → `_primary_takeover_active() = True`. Mỗi tick chạy đúng thứ tự:

```
1. legacy.hard_preemptive_decision()   → nếu có (safety/segment) → THẮNG tuyệt đối, dừng
2. proposal = director_v2_shadow.propose_current()
3. selection = director_v2_takeover.evaluate(proposal)
4. selection == hard_hold             → WAIT ("director_v2_hard_hold")
5. accepted & owner=director_v2 & proposal khớp → materialize() → DECISION CỦA V2   [selected++]
6. còn lại (proposal thiếu/invalid/materialize fail) → legacy.decide()               [fallback++]
```

Tức: **Director V2 là primary** — tự materialize câu public khi proposal hợp lệ. **Legacy Compatibility
Director** giữ 2 vai: (a) hard-preemption safety/segment luôn thắng trước; (b) fallback khi V2 không dựng
được câu. **Cognitive Brain** shadow thuần, offer sau execute, không bao giờ public.

Tỷ lệ V2-vs-legacy thực tế đọc qua metric `director_v2_primary_selected_total` /
`director_v2_primary_fallback_total` khi chạy live.

### 4.3. Delivery transaction

```
turn_lock (mỗi lúc 1 lượt)
  → coordinator.reserve(idempotency_key)   # trùng key → bỏ qua, chống nói 2 lần
  → _execute: dựng user_text + context + mood directive → LLM stream → parse → rule_filter (regen nếu phạm)
             → TTS phát + subtitle + VTS
             → verify đã giao?  có → commit → project vào World/Self ("Mai vừa nói X")
                                không → release, KHÔNG ghi là đã nói
  → pool.remove(tin đã đáp)
```

### 4.4. Tự nói (dead-air)

Hồ nguội + `urge.should_speak_now()` → Director chọn `SELF_TALK` → `autonomy.force_generate` (dựng chủ đề
từ goal/thread/lore, **không bịa**) → cùng đường transaction 4.3.

---

## 5. Config & feature flags

31 YAML trong `config/`; 50 cờ trong `features.yaml` (mỗi cờ có `enabled` state, `depends_on`,
`vram_cost_mb`). Owner chính:

| YAML | Owner cho |
|---|---|
| `system.yaml` | product version, app-level |
| `state.yaml` | giới hạn ingress/Agent/World/Self/Relationship (canonical sau S2) |
| `cognition.yaml` | context projection, Cognitive Context, Brain adapter |
| `kernel.yaml` | tick owner, `rollout_mode` (public rollout của Brain) |
| `director.yaml` | Director loop, `director_v2_takeover.ownership_mode` |
| `execution.yaml · capabilities.yaml` | transaction, capability registry |
| `emotion_appraisal · mood_engine · mood_style · affect_v2` | mood 2 kênh |

Ngưỡng/TTL/cooldown/weight production nằm trong YAML, không hardcode. Feature/critical config fail-closed
với scalar sai kiểu, dependency/conflict sai, resource budget vượt.

Memory runtime đọc bound/capacity từ `system.yaml::memory`: SQLite semantic evict oldest khi vượt
`semantic_max_entries`, query semantic bị cắt ở `query_timeout_s=0.15`, cửa sổ tính p95 có giới hạn,
episodic summary có cadence/capacity/TTL/input-output/timeout/pending bound cùng trọng số recency-salience,
và `features.yaml::memory_semantic` + `features.yaml::episodic_memory` là các master flag tương ứng.
Flag tắt hoặc semantic không khởi động được đều giữ working memory hoạt động; config/contract sai vẫn
fail-closed khi composition.

---

## 6. An toàn, riêng tư, phục hồi

- **Safety preempt** đứng trước mọi soft policy (V2/legacy scoring không ghi đè được).
- **Emergency latch** fail-closed cho speech + environment action.
- **Capability** từ chối mặc định; thiếu bằng chứng → không khả dụng.
- **Transaction**: không commit state trước verified success.
- **Credential**: chỉ qua environment/secret store lúc chạy; không ghi vào YAML/CLI/`.env.example`/Git.
  `MAI_DASHBOARD_CONTROL_TOKEN` bắt buộc khi dashboard bật; `DISCORD_BOT_TOKEN`/`OBS_WEBSOCKET_PASSWORD`
  chỉ khi consumer bật.
- **Privacy**: memory/relationship/journal lưu bí danh + ý nghĩa, không PII người xem và không transcript.
  Query text không được ghi vào log/metric semantic memory.
- **Shutdown**: tắt có thứ tự, idempotent, snapshot runtime atomic.

---

## 7. Kiểm thử & cổng phát hành

- CI: GitHub Actions `windows-latest`, `pytest tests -m "not llm and not slow"`, junit artifact.
- Offline regression đầy đủ (`v2.0\venv`): 2.288 test, 0 lỗi (post-S8, `pytest tests -q`).
- Marker: `slow` (leak/soak), `llm` (cần llama-server thật) — skip được.
- Release `2.0.0` **chưa đạt gate**: thiếu live/LLM acceptance, audio/VTS/OBS canary, human/rollback
  evidence. Release gate tự đọc + verify source/hash/freshness, fail-closed.

---

## 8. Trạng thái năng lực hiện tại

"Có mã" ≠ "đã ghép" ≠ "đã test" ≠ "đã phát hành". Bảng theo config working tree hiện tại:

| Năng lực | Trạng thái | Ghi chú |
|---|---|---|
| Hội thoại chat → nói (V1 path) | **LIVE** | product 1.4.3, regression rộng |
| Director V2 (quyết định câu) | **LIVE** | primary test-cutover, legacy fallback; chưa live canary |
| Speech / Avatar typed adapter | **LIVE (test)** | fail-safe khi VTS thiếu |
| Mood Hybrid (v1+v2) | **LIVE** | Kênh A số deterministic; Kênh B sắc thái LLM |
| World / Self Model | SHADOW | read-only, không đổi quyết định |
| Cognitive Brain | SHADOW | proposal-only, offer sau execute, chưa public |
| Semantic memory | **LIVE** | mặc định qua `memory_semantic`; timeout/startup fail → working-only |
| OBS scene action + perception | OFF | có executor thật, chưa credential/canary |
| Goal proposals · closed-loop canary | OFF | chưa vào vòng quyết định tự chủ |
| Release 2.0.0 | CHƯA | thiếu live/LLM/human/rollback evidence |

Chỉ code + composition + test + release evidence tương ứng mới được nâng một dòng từ trạng thái này sang
trạng thái cao hơn.

---

## 9. Từ điển thuật ngữ (chống nhầm)

| Thấy trong code/metric | Đừng hiểu thành | Nghĩa thật |
|---|---|---|
| `public_owner = COMPATIBILITY` | Legacy Director đang quyết | "đường DirectorLoop (không phải Brain)" — bên trong V2 vẫn primary |
| `cognitive_brain_shadow` | Brain chạy chính | Brain offer sau execute, chỉ ghi shadow, không ra loa |
| `director_v2_takeover` | V2 đã takeover production | test-cutover, có legacy đỡ mỗi khi fail |
| `rollout_mode: shadow` (kernel) | Brain shadow ⇒ legacy public | Kernel route public sang DirectorLoop (nơi V2 primary), Brain shadow |
| "compatibility" (2 nghĩa) | luôn cùng một thứ | (a) đường DirectorLoop trong kernel; (b) legacy Director trong DirectorLoop |

---

*Cập nhật đặc tả này trong cùng change khi sửa behavior. Scope/thứ tự tương lai thuộc `ROADMAP.md`, không
đưa kế hoạch vào đây như production.*
