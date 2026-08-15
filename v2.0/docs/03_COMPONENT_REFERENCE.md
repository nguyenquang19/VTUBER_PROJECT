# 03 — Component reference

> **Applies to:** Mai `1.4.3` (baseline `1.0.0`)
>
> Dùng tài liệu này để tìm owner; không đặt behavior mới vào file tiện tay gần nhất.

## 1. Composition và orchestration

### `orchestrator/stream_runtime.py`

Vai trò: composition root của live system. `build_stream_runtime()` load feature status, dựng service,
start llama.cpp, wire callback và trả `StreamRuntime`. `StreamRuntime.start()/stop()` quản lifecycle.

Từ `1.3.1`, các helper composition thuần nội bộ được tách theo ownership:

- `orchestrator/runtime_tts.py`: startup/health/degraded gate của TTS stack;
- `orchestrator/runtime_feature_bindings.py`: đăng ký callback enable/disable/health;
- `orchestrator/runtime_operations.py`: dashboard, recovery, emergency và shutdown wiring.

Các module này không phải composition root mới và không được tự đổi thứ tự lifecycle.

`orchestrator/main.py` chỉ là compatibility shim fail-fast cho command cũ. Nó không được import hoặc
compose dashboard, state machine, trigger manager hay service production.

Input: `ConfigLoader`, danh sách `InputService`, `StreamRuntimeConfig`.

Output: runtime đang chạy, operations snapshot và các side effect platform/audio/dashboard.

Khi sửa:

- Chỉ nối dependency ở đây; không đặt business decision mới vào composition root.
- Service I/O mới phải start trước consumer và stop theo thứ tự ngược.
- Mọi private access chéo subsystem là dấu hiệu cần public method/interface.
- Config critical phải validate trước khi start process/model.

### `orchestrator/config_loader.py`

Load YAML theo logical name, hỗ trợ dotted path và hot reload atomic. Parse lỗi giữ config cũ. Thêm
file YAML mới phải đăng ký trong `CONFIG_FILES`; field runtime critical cần thêm vào validation model.

### `orchestrator/features.py`

Quản lý feature graph, dependency, conflict, VRAM budget và handler enable/disable/health. Core feature
không toggle; feature mới phải có config và ít nhất một metric.

### `orchestrator/event_bus.py`

Pub/sub async bounded. Queue overflow theo `drop_oldest` hoặc `drop_newest`; không dùng EventBus làm
nguồn dữ liệu bền vững. Subscriber chậm không được block producer.

## 2. Input subsystem

### Platform adapters

| File | Input | Output | Failure chính |
|---|---|---|---|
| `services/input/youtube_chat.py` | video ID, pytchat stream | `InputEvent(CHAT_YOUTUBE)` | video end/404/rate limit |
| `services/input/discord_chat.py` | env token, channel IDs | `InputEvent(CHAT_DISCORD)` | token/intent/permission/queue full |

Adapter phải giữ platform SDK ở boundary, chuẩn hóa timestamp/metadata và không log credential.

### `services/input/chat_router.py`

Start emotion + source consumer. Mỗi event được emotion-classify rồi đi vào Director intake hoặc
legacy FIFO runner. Router ghi activity, grounded event và speech-completed event. Nếu delivery trả
`delivered=false`, không được ghi speech completed.

Điểm debug: `_handle_event()`, `_to_emotion_event()`, intake callback, metrics `router_*`.

## 3. Emotion và Hybrid affect

### `services/emotion/classifier.py`

Chuyển text/metadata thành `EmotionEvent` category và cause đã sanitize. Category là chìa khóa tune
response strategy; classifier sai sẽ kéo theo mood/directive sai.

### `orchestrator/mood_engine.py`

Legacy mood nhiều chiều `vui/buon/buc/bon_chon/nguong`, có position/target/decay. Đây là nguồn tone
được giữ để rollback và cho hai nhóm Hybrid cần sắc thái mềm.

### `services/emotion/affect_v2.py`

Duy trì `TurnAffect` có TTL và `SessionMood` decay theo elapsed time. Cause chỉ giữ bounded
`source_event_id`, không giữ raw viewer text.

### `services/emotion/hybrid_affect.py`

Tạo đúng một `ResponsePlan`:

- compliment và genuine sad share lấy tone legacy với response mode mềm;
- mention/troll/spam/jailbreak/sexual/donation/recovery lấy strategy v2;
- tone flag cụ thể có thể override style;
- không đổi safety, donation hoặc goal priority.

### `orchestrator/emotion_orchestrator.py`

Chạy legacy và v2 trên cùng event, expose snapshot và delivery directive. Rollback tức thời bằng
`mood_v2_prompt=false`; legacy mood engine không bị xóa.

Khi mood có bug, kiểm tra theo thứ tự: classifier category → processed cause → legacy position/target
→ TurnAffect/SessionMood → Hybrid ResponsePlan → prompt directive.

## 4. Agent state, goal và continuity

| Component | Trách nhiệm | Không được làm |
|---|---|---|
| `EventLedger` | recent grounded events, dedup/bounds | lưu transcript vô hạn |
| `AgentState` | reducer tạo topic/phase/environment/last speech | tự quyết định action |
| `GoalManager` | active/candidate/suspended goal, TTL/status | cho LLM tự commit goal |
| `AgendaPolicy` | xếp thứ tự goal/thread grounded | để mood đổi hard priority |
| `OpenThreadManager` | thread đang mở và evidence | tạo fact không provenance |
| `ConversationContextComposer` | context bounded liên quan query | dump toàn state vào prompt |
| `ConversationRepairPolicy` | nhận biết contradiction/uncertain recall | bịa fact để lấp chỗ trống |
| `RelationshipManager` | profile/note/narrative/gag pseudonymous | lưu PII/raw ID trực tiếp |

Source files nằm trong `services/agent/` và `services/relationship/`. Snapshot types bất biến nằm ở
`services/agent/types.py`, goal types ở `services/agent/goal_types.py`.

## 5. Director subsystem

### `services/director/salience.py`

Pool chat bounded, chấm salience, decay, cluster/dedup. Donation/direct mention và recency ảnh hưởng
candidate score theo config. Nếu spam làm mất chat quan trọng, kiểm tra capacity, decay, cluster và
eviction trước khi sửa Director.

`kind_detection` trong `chat_salience.yaml` nhận diện mention/question bằng pattern cấu hình, gồm câu
hỏi tiếng Việt không có dấu `?`. Chat thường vẫn vào pool để gom cụm; không đồng nghĩa nó được mở turn.

### `services/director/chat_pulse.py`

Đo nhịp chat theo cửa sổ thời gian và tạo pulse state. Pulse ảnh hưởng pacing/hosting, không phải
nguồn truth về nội dung.

### `services/director/director.py`

Pure decision layer: `DirectorInput -> DirectorDecision`. Hard arbitration gồm safety hold,
donation/operator state; soft arbitration gồm salience, goal/thread, proactive candidate và cooldown.
Unit test của Director nên dùng snapshot deterministic, không gọi LLM/TTS.

Feature `director_chat_gate` so top score với `director.min_actionable_score`. Candidate thấp điểm trả
`WAIT/below_actionable_score`, trừ khi backlog đủ lớn để `SUMMARY`; feature OFF khôi phục hành vi cũ.
Hết thời lượng segment vẫn transition dù pool còn chat, sau khi hard priority và goal đã được xét.

`COLD` là trạng thái kéo dài, không phải edge event: nó chỉ cho phép silence self-talk sau
`dead_air_seconds`. Sau một self-talk đã delivery, `self_talk_cooldown_seconds` chặn mọi silence/urge/mood
self-talk mới cho tới hết khoảng nghỉ; break sau chuỗi read-chat vẫn được ưu tiên để tránh đọc FIFO vô hạn.
Không có material hoặc delivery thất bại không được cập nhật `last_spoke`.

`SUMMARY/VIBE` dùng `room_reaction.cooldown_seconds` độc lập với self-talk. Director chỉ chặn nhánh phản
ứng phòng chat; hard priority, donation, question và mention vẫn được xét bình thường. Cooldown chỉ được
đánh dấu sau delivery thành công. Metric phải phân biệt `cooldown_blocked` với candidate bị dedup/suppress.

### `services/director/v2_shadow.py`

Director V2 shadow tạo proposal deterministic từ World/Self/Capability snapshot và candidate bounded, rồi chỉ lưu structured record. `HardArbiter` thắng soft score theo emergency → operator → safety → permission → transaction conflict → critical state → donation. Service không reserve transaction, không gọi executor/LLM/TTS và không được truyền vào `Director` hoặc `DirectorLoop`; feature `director_v2_shadow=false` là rollback tức thời.
### `services/director/v2_takeover.py`

Agreement-only gate cho rollout conversational V2. Nó chỉ accept khi action V2 khớp action legacy, stage YAML cho phép và chat/thread/goal evidence có trong cùng `DirectorInput`; nếu không giữ nguyên `DirectorDecision` legacy. Selector không reserve, generate, deliver hoặc commit. `director_v2_takeover` mặc định tắt; bật/tắt là rollback tức thì.
### `services/director/director_loop.py`

Driver tick và transaction owner. Flow chính: evict stale → build input → decide → record decision →
reserve → generate → deliver → commit/release. Từ `1.3.1`, rendering prompt/action thuần nằm ở
`services/director/action_prompts.py`; deferred runner + typed delivery mechanics nằm ở
`services/director/delivery_boundary.py`. `DirectorLoop` vẫn là owner duy nhất phối hợp business side
effect sau delivery; hai helper không tự commit goal/thread/pool.

DirectorLoop sở hữu cửa sổ dedup bounded của output `SUMMARY/VIBE`. Nó finalize candidate đầu là
delivery failure trước khi regenerate; candidate thứ hai còn trùng thì bị suppress, transaction được
release và backlog giữ nguyên. Chỉ text đã delivery mới được ghi vào cửa sổ, purge backlog và bắt đầu
cooldown. `action_prompts.py` tạo correction prompt từ candidate bị từ chối và một số output gần đây đã
giới hạn bởi config; prompt helper không sở hữu state hoặc side effect.

DirectorLoop còn sở hữu cửa sổ `speech_dedup` chung cho `READ_CHAT` và `CONTINUE_THREAD`. Cửa sổ này
so token không phụ thuộc thứ tự nên bắt được trường hợp cùng hai ý nhưng đảo câu. Mỗi candidate chỉ được
regenerate một lần; bản sửa vẫn trùng không tới TTS và không commit. Metric tách generated, duplicate,
regenerated, suppressed và recent-count để operator phân biệt chất lượng output với lỗi delivery.
Candidate sửa vẫn trùng đi qua context quarantine: ref bị loại, thread resolve và goal cancel với reason
`speech_duplicate`. Metric quarantine riêng phải chứng minh không còn retry vô hạn; candidate không được
ghi history hoặc speech-completed.
Focused continuation is excluded from that quarantine path and follows the delivered `park` fallback
defined in the Conversation Thread Engine section below.

`services/director/speech_style.py` là pure bounded policy cho public speech. Nó chuẩn hóa opener theo
phrase cấu hình, nhận diện câu hỏi tiếng Việt, giữ cửa sổ chỉ từ delivery thành công và trả reason typed
cho DirectorLoop; nó không gọi LLM, TTS hoặc commit state. DirectorLoop inject constraint trước generation
và cho đúng một correction attempt khi style vượt ngân sách. Exhausted opener/question correction là
fail-open có metric; exhausted sentence/word shape được clamp theo câu hoàn chỉnh, khác semantic
duplicate là suppress + quarantine. Guard áp dụng cho `READ_CHAT`,
`CONTINUE_THREAD` và `SUMMARY/VIBE`; self-talk tiếp tục dùng stage validation + opener tracker riêng.
Mọi delivery, gồm self-talk, vẫn record vào global semantic/style history; self-talk trùng global
semantic history bị suppress trước TTS để tránh lặp chéo action.
`director_execute_failed_total` tăng tại outer action boundary; stress report phải fail khi counter này
khác không, kể cả các content/latency gate còn lại xanh.

`ActionContextBuilder` diễn giải conversation move thành ràng buộc public: mặc định một đến hai câu,
`deepen/clarify/compare/summarize/park` không tự hỏi ngược; riêng `invite` mới yêu cầu đúng một câu hỏi.
Context vẫn bounded và chỉ dùng fact của thread.

### `services/autonomy/self_talk_planner.py`

Thought Engine nội dung có state bounded, không phải scheduler. Nó dựng `Thought` từ cause thật theo
thứ tự grounded material, environment, recent context rồi silence; mỗi thought có intention và
evidence refs. `cognitive_moves` là thao tác nhận thức chung như chú ý/so sánh/đặt giả định, không phải
topic hay seed nội dung. Thought có recent/environment material dùng arc `open`, `develop`, `invite`,
rồi `wait`; riêng cause `silence` là one-shot một lần cho mỗi quiet episode để không kéo dài một premise
rỗng. Thought Ledger bounded chống lặp cả intention lẫn lời đã nói.

`validate_output()` enforce số câu, câu hỏi theo nghĩa (không chỉ dấu `?`), đúng một câu hỏi ở `invite`
và coverage chống chép lại stage trước; Director chỉ regenerate một lần. `can_deliver()` chặn pending
ambient nếu chat đến trong generation. Mọi chat mở global quiet gate; chat suspend arc `open/develop`
thay vì xóa, nhưng resolve arc đang `wait`. `prepare/release/commit` bảo đảm chặng chỉ tiến khi delivery
trả `delivered=true`.

### `services/autonomy/lore_material.py`

Đọc bullet từ các section được allowlist trong cùng `llm_main.lore_prompt_path`, chuẩn hóa và cap anchor
trước khi cấp cho Thought Engine. Provider chỉ có một reservation pending, chống lặp bằng cửa sổ bounded
và chỉ advance cursor khi lượt lore đã delivery. Missing/empty/malformed lore là no-material fail-safe,
không được biến prose ngoài allowlist thành fact. Feature runtime: `self_talk_lore`; metrics reserve,
commit, release và unavailable được gộp vào snapshot của self-talk planner.

Mood đi qua `MoodStyleTable` để đổi thái độ, nhịp và độ dài. Mood không tạo thought cause, không chọn
lại lịch sử, không thêm người/game/sự kiện và không làm thay đổi hard priority. Feature runtime:
`self_talk_planner`.

### `services/director/action_transaction.py`

State machine bounded với idempotency cache. Illegal transition phải fail; duplicate committed không
deliver lại. Snapshot chỉ giữ recent transaction theo giới hạn YAML.

### `services/director/decision_record.py`

Audit bounded cho action/reason/evidence/candidate/transaction result. Không chứa raw chat, token hoặc
viewer identity. Dashboard render view model này thay vì tự đoán reason.

## 6. LLM subsystem

### `services/llm/process_manager.py`

Tạo command `llama-server.exe`, quản process do runtime sở hữu, chờ health và stop process đã tạo.
Không dùng Ollama/transformers/vLLM. Nếu server có sẵn, manager không được giết process operator-owned.

### `services/llm/llama_cpp_llm.py`

HTTP streaming client cho llama.cpp. Input `LLMRequest`, output ordered `LLMToken`. Theo dõi TTFT,
token count, decode throughput và cancel request.

### `services/llm/prompt_manager.py`

Load persona system prompt, giữ history bounded, ghép mood/agent/memory/relationship/action context.
Persona prefix cần byte-stable để tận dụng cache. Không commit history ở đây trước delivery. Từ `1.3.0`,
`PromptCache.from_loader` nối persona + lore (`llm_main.lore_prompt_path`) thành cùng prefix; cả hai tĩnh
nên prefix vẫn byte-stable. Cờ `llm_main.inject_mood_directive` (mặc định true) bật/tắt mood directive
trong `[Context]` block.

### `services/llm/llm_turn.py`

Điều phối một turn: build request → fallback generation → parse → filter/regenerate → metrics/log →
pending delivery. `finalize_delivery(request_id, success)` là ranh giới commit history/memory.

### Parser/filter/fallback

- `services/llm/parser.py`: strip reasoning/meta, parse output contract.
- `services/filter/rule_filter.py`: deterministic safety/persona rules.
- `services/filter/regenerator.py`: generation lại khi verdict yêu cầu.
- `services/llm/canned_response.py`: fallback cuối không cần primary model.
- `orchestrator/fallback_manager.py`: chain handler có timeout từng level.

`DirectorLoop` treats the final filter verdict as a delivery gate: an exhausted
`regenerate`, `replace`, or `block` verdict never reaches TTS and never commits a transaction.
The rejected chat/thread/goal is quarantined after the bounded attempts so it cannot be selected on
every following tick. Candidate filter hits and delivered-output violations are separate metrics.

## 7. TTS subsystem

| Component | Input | Output | Chú ý |
|---|---|---|---|
| `sentence_splitter.py` | Vietnamese text | ordered sentences | sửa splitter phải test viết tắt/dấu câu |
| `vieneu_service.py` | `TTSRequest` | `AudioChunk` stream | enroll reference trực tiếp vào RAM một lần khi start |
| `audio_player.py` | chunks | sound device | queue bounded, cancel, no-overlap; optional `tts.pitch_semitones` pitch-shift trước khi phát |
| `subtitle_fallback.py` | sentence request | empty final chunk + file/event sink | file atomic, require real sink |
| `tts_pipeline.py` | full response text | `TTSDeliveryResult` | lock synth, fallback per sentence, hỗ trợ subtitle-only |
| `natural_timing.py` | action/context/latency | timing plan | config-driven, không che LLM latency |

## 8. Memory và relationship

- `WorkingMemoryService`: in-memory recent entries, không bền qua restart.
- `SemanticMemoryService`: embed bằng BGE-M3 và lưu vector/metadata SQLite; optional.
- `MemoryFallbackManager`: semantic lỗi thì fallback working memory.
- `MemoryExtractor`: chỉ extract từ committed viewer turn; ambient không tạo memory fact.
- `RelationshipStore/Manager`: SQLite profile/note/narrative/running gag, TTL và consent config.

Memory không được block hoặc làm fail turn chính. Fact prompt phải có provenance/confidence phù hợp.

## 9. Dashboard và operations

### `dashboard/dashboard_server.py`

FastAPI server, `/operator`, `/legacy`, `/api/snapshot` và WebSocket. Operator v2 lấy
`operator_overview` dựng server-side. Mutating endpoint gọi control plane/manager public API; standalone
mode khóa mutation.

GPU utilization và VRAM trong dashboard lấy từ `nvidia-smi` với timeout/refresh trong `system.yaml`.
Query lỗi phải trả unavailable/stale rõ ràng, không sinh số demo.

Operator v2 render mood từ `mood_pos` float thành năm cột realtime; `current_mood` integer chỉ dùng cho
policy/prompt và không đủ mịn cho animation. Vạch trắng là target, chiều cao cột là position. Snapshot
đính `sampled_at` và `ticks`; standalone root vẫn mở operator v2 nhưng ghi rõ runtime offline.

Trang mặc định của Operator v2 là **live cockpit**, không phải trang tổng hợp kỹ thuật. Trong một viewport
desktop, operator phải thấy được: trạng thái runtime/WebSocket, hành động và câu Mai đang nói, chat/thread
đang được xử lý, delivery state, queue/backpressure, độ trễ LLM/TTS và incident cần can thiệp. ID kỹ thuật,
evidence đầy đủ, feature flags, mood chi tiết và review queue nằm ở view chẩn đoán phụ. Giao diện chỉ render
source snapshot; trạng thái thiếu dữ liệu phải hiển thị `—`/unavailable rõ ràng, không tự suy luận hay tạo số.

Các control có side effect giữ nguyên public endpoint. Pause/resume phải phản ánh khả dụng từ snapshot;
emergency stop phải nổi bật nhưng không được kích hoạt do click nhầm. Operator v2 dùng nhãn tiếng Việt nhất
quán, phân cấp critical/warning/healthy bằng cả chữ và màu, hỗ trợ keyboard focus và co về layout một cột
trên viewport hẹp. `/legacy` tiếp tục là rollback UI độc lập.

Dashboard độc lập chạy ở cổng riêng và dùng source mode `auto | live | history`. `auto` ưu tiên snapshot
live từ dashboard embedded trên loopback; khi upstream không khả dụng thì chuyển sang final snapshot +
journal local mà không giả runtime online. Source mode, freshness và controls availability phải nằm trong
snapshot trả cho browser. Command online chỉ được proxy tới allowlist endpoint loopback; history/offline
luôn read-only. Vì dashboard độc lập giữ cổng riêng, runtime có thể bật hoặc tắt sau đó mà không xung đột
bind port.

### Operations services

| File | Trách nhiệm |
|---|---|
| `health_supervisor.py` | bounded restart/backoff/circuit breaker |
| `shutdown_coordinator.py` | ordered best-effort graceful shutdown |
| `emergency_control.py` | đóng speech/action gate, cancel, resume prune stale |
| `control_plane.py` | pause/resume/cancel/pin và audit |
| `incident_log.py` | append-only sanitized incident ledger |
| `post_stream_review.py` | kiểm tra snapshot/audit/incident/soak |
| `standalone_snapshot.py` | đọc snapshot/log khi runtime offline |
| `dashboard_data_source.py` | auto-switch live/history, query journal bounded và proxy command allowlist |

## 10. Evaluation và data tools

`services/evaluation/` chứa scenario loader/harness, deterministic simulator, acceptance runner,
human review, mood A/B, data quality và candidate readiness. Đây là offline tooling; không thêm LLM
shadow call vào live path. Scripts tương ứng nằm trong `scripts/`.

| Component | Trách nhiệm | Boundary quan trọng |
|---|---|---|
| `services/evaluation/data_quality.py` | contract, quality gate, delivery join, canonical projection, session split | không mutate/relabel raw record |
| `services/data/record_schema.py` | wire-format model (turn/delivery/canonical) + fingerprint + registry | source of truth định dạng journal; `extra="forbid"` |
| `eval/contracts/mai_agent_v1.yaml` | compatibility matrix và schema version | source of truth data contract |
| `scripts/export_dataset.py` | raw → canonical/SFT/DPO immutable bundle | chỉ nhận explicit `delivered=true` |
| `scripts/backup_data.py` | backup multi-source + SHA-256 manifest | copy-only, không xóa source |
| `scripts/restore_data.py` | verify/apply restore | verify-only mặc định, refuse overwrite |
| `scripts/simulate_youtube_replay.py` | deterministic full chat timeline | stub delivery, không chấm naturalness |
| `scripts/stress_youtube_llm.py` | replay với llama.cpp/filter thật | delivered flags tách candidate flags |
| `scripts/stress_youtube_tts.py` | phát lại output đã delivery qua VieNeu thật | đo/huỷ PCM, không mở loa; queue model không giả là audio đã phát |
| `scripts/stress_youtube_live_pipeline.py` | wall-clock chat → Director → LLM → TTS → playback | input chạy đồng thời; backend im lặng chặn theo PCM |
| `scripts/check_finetune_readiness.py` | kiểm tra đủ data/review gate | không tự cutover model |

`orchestrator/logger.py::TurnLogger` ghi generation attempt và delivery outcome ra hai journal riêng.
Từ `1.1.0`, nó validate mỗi record theo model trong `services/data/record_schema.py` trước khi ghi;
record không khớp đi vào `logs/quarantine.jsonl` (metric `turn_quarantined_total`), không lọt vào journal
train. `LLMTurnRunner.finalize_delivery()` là writer của authoritative outcome trên Director path;
exporter join bằng `session_id + request_id + turn_id`. Khi mood/engine bug làm đổi shape record, đọc
quarantine journal trước để thấy field lệch.

## 12. Animation subsystem (VTube Studio)

`services/animation/` là adapter avatar production từ `1.1.0`, gate qua feature `animation_smooth`.

| Component | Trách nhiệm | Boundary quan trọng |
|---|---|---|
| `services/animation/vts_transport.py` | websocket VTS: connect, auth token-persist, load/trigger hotkey | config-injected; không business logic |
| `services/animation/vts_service.py` | `AnimationService`: `MoodState.dominant()` → hotkey sau `DELIVERED` | fail-safe, không giết turn; `sync_with_audio` no-op |
| `config/animation.yaml` | host/port/plugin/token, `mood_hotkeys`, `retrigger_on_same_mood` | đăng ký trong `CONFIG_FILES` |

`director_loop.py` gọi `express()` sau khi transaction đạt `DELIVERED` — side-effect đúng invariant
"sau delivered". VTS không kết nối → service `degraded`, turn vẫn chạy bình thường. Lip-sync không đi qua
API (VTS lấy từ audio input), nên `sync_with_audio` là no-op có chủ đích. Feature runtime: `animation_smooth`.

## 11. Conversation Thread Engine

The long-form continuity path extends the existing `OpenThreadManager` rather than creating a second
conversation store:

- `topic_matcher.py`: deterministic lexical match with a hard reject threshold;
- `conversation_move_planner.py`: chooses `deepen`, `clarify`, `compare`, `invite`, `summarize`,
  `resume`, and other public conversation moves;
- `thread_detector.py`: converts grounded chat and delivered speech into lifecycle signals;
- `open_thread_manager.py`: owns bounded evidence, claims, viewer contributions, open questions,
  active/waiting/parked status, TTL, and terminal history;
- `conversation_context.py` and `action_context.py`: expose only bounded facts and the selected public
  move to llama.cpp;
- `director_loop.py`: publishes spoken progress only after typed delivery success.

Production đặt ngân sách move thấp để một thread được phát triển, tóm tắt rồi park thay vì chiếm phần
lớn delivery trong phòng chat nhanh. Thay đổi ngưỡng phải được replay cùng tỷ lệ `continue_thread`,
không đánh giá chỉ bằng số câu hay word count.

`conversation_continuity` is the owning feature toggle. Topic matcher and move planner lifecycle is owned
by `OpenThreadManager`; their counters are included in thread metrics.

`GoalManager` owns delivery-focused continuity. A chat-derived `CONTINUE_THREAD` goal waits until its
source chat is actually delivered. `DirectorLoop` then focuses that parent thread, removes stale soft
continuations for other parents, and `GoalManager` creates one atomic same-parent successor after each
delivered move until `park`, `close`, or wait-for-chat. This is a coherence rule, not a quota: Mai may
use several continuation turns when the move planner requires them.

`DirectorLoop` clears pending soft continuations only after a delivered room reaction. Parked threads
remain bounded session state, but `ProactiveHostingPolicy` ignores them; they resume only from later
grounded chat matching. Metrics and stress reports may expose continuation share for diagnosis, but
that share is not an acceptance threshold by itself.

Exhausted semantic duplication on a focused continuation uses one directed `park` generation rather
than context quarantine. This preserves the public closing sentence: the parent/goal is released only
when that fallback reaches delivery. `director_thread_forced_park_total` counts successful fallback
closures; an unsuccessful fallback does not advance thread state.

Before every Director arbitration, `GoalManager.reconcile_threads()` cancels thread-bound goals whose
parent is no longer present. This housekeeping must run before `snapshot()` so an expired thread cannot
leave a `CONTINUE_THREAD` goal blocking live chat until its independent TTL expires.

Temporal words such as `nãy/lúc nãy` do not select the most recent thread by themselves. They require a
lexical topic match; only explicit continuation phrases such as `kể tiếp/nói tiếp/rồi sao` may use the
single-active-thread fallback. This prevents fast-room remarks from hijacking an unrelated thread.

`scripts/stress_youtube_llm.py` reuses the full replay/Director path but replaces the offline stub with
the production llama.cpp turn runner and rule filter. It records every generation, final delivery,
fallback, TTFT/decode rate, thread outcome and a bounded operator-review sample.
The full-delivery trace reports topic-boundary violations separately: continuation before its source
chat was delivered, continuation on a different parent before the current parent parked, room reaction
before `park`, and automatic old-thread continuation after room reaction. Every such count is a hard
zero gate; `continue_thread` ratio remains descriptive.

Quality gates inspect only outputs that crossed the delivery boundary; rejected candidates remain in
`candidate_flags` for safety observability and filter tuning.

`scripts/stress_youtube_tts.py` nhận report của `stress_youtube_llm.py`, giữ đúng thứ tự/timestamp các
output đã delivery rồi chạy text qua `VieNeuTtsService -> TTSPipeline` thật. Measurement player chỉ thu
metadata chunk và huỷ PCM nên không mở thiết bị âm thanh. Report tách metric synth (startup, TTFA, RTF,
audio duration, silent turn, failure/fallback) khỏi playback queue model (start delay, backlog, final drain).
Output đã delivery nhưng sentence splitter không tạo câu/audio phải bị tính là silent turn, không
được ẩn sau gate số mẫu tối thiểu. Khi cấu
hình giữ llama.cpp resident, runner chỉ giữ model trong VRAM; nó không sinh lại nội dung và không sửa
report nguồn. Checkpoint chỉ chứa record đã synth thành công/thất bại và phải resume theo request ID.

`scripts/stress_youtube_live_pipeline.py` sở hữu bài stress wall-clock end-to-end cho corpus replay.
Runner ghép input paced, production `ChatRouter`, `DirectorLoop`, llama.cpp, filter, `TTSPipeline` và
`AudioPlayer`. Playback backend im lặng chỉ chặn theo số sample PCM và phát event start/end;
không import hay mở `sounddevice`. Runner quan sát queue/pool và ghi report atomic, không sửa
production state, database hoặc corpus nguồn.
Report tính quality aggregate trên toàn bộ output trước khi cắt sample audit, gồm semantic duplicate,
room-reaction cadence, tỷ lệ `continue_thread`, formula-opener ratio và question-ending ratio; sample
giới hạn không được dùng thay cho aggregate. Foreign-identity evaluator phải phân biệt direct user turn
với system-only directed continuation: một tên ngoài chỉ xuất hiện trong thread context không đủ để kết
luận Mai tự nhận danh tính đó. Human review phát hiện engagement pressure phải được thêm vào
`filters.yaml::manipulation` và gate LLM; không coi technical style pass là đủ nếu câu yêu cầu viewer
tương tác để được ưu tiên hoặc nói cả phòng đang đánh giá thái độ của họ.
