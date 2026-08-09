# Master Plan — Mai Host Agent Maturity

> **Mục tiêu:** nâng Mai từ một AI VTuber phản hồi chat tốt thành một host agent local có trạng thái liên tục, mục tiêu riêng, hội thoại nhất quán, hành động dựa trên môi trường thật, và quy trình vận hành/data đủ chín để fine-tune.
>
> **Đây là spec thực thi.** AI agent phải làm đúng thứ tự M0 → M8; không nhảy milestone, không gộp task. Một task = một commit + test tương ứng + cập nhật `STATE.md`/dev manual khi logic đã đổi.

---

## 0. North star, phạm vi và nguyên tắc

### 0.1. Đích sản phẩm

Mai phải có thể:

1. Đọc và ưu tiên chat/donation hợp lý, nói đúng nhịp host.
2. Giữ được chủ đề, câu hỏi, lời hứa và câu chuyện dang dở qua nhiều lượt.
3. Có **một agenda hiện tại** và chủ động hành động vì agenda đó, thay vì tự nói chỉ để lấp im lặng.
4. Phản ứng với trạng thái thật của một môi trường (ban đầu chỉ **một** môi trường: game *hoặc* browser/video *hoặc* OBS).
5. Có mood, relationship và narrative nhưng mọi fact đều có nguồn; không bịa viewer, sự kiện, quan hệ hoặc ký ức.
6. Vận hành live an toàn, quan sát được, recover được; dữ liệu thu được đủ sạch để train sau này.

### 0.2. Không phải mục tiêu

- Không cố sao chép nội bộ của Neuro-sama; kiến trúc nội bộ của sản phẩm khác không phải spec đáng tin.
- Không tạo "inner monologue" tự do, liên tục bằng văn bản/chain-of-thought.
- Không cho LLM chạy shell, browser hoặc game action tùy ý.
- Không xây social lore hay drama giả để trông có đời sống.
- Không fine-tune khi input/prompt/goal policy còn thay đổi mạnh.
- Không thay llama.cpp, Windows, Gemma/VieNeu stack nếu chưa có spike report theo `docs/CLAUDE.md`.

### 0.3. Kiến trúc đích

```text
Input events ──┐
Environment ──┼─> EventLedger ─> AgentState <─> GoalManager
Mood events ──┘                       │               │
                                     BrainLoop ───────┘
                                          │  (structured candidates; no speech)
                                          v
SaliencePool + ChatPulse ───────> Director / Action Arbiter
                                          │
                        ActionDecision: read | follow-up | continue-thread
                                  | observe | environment action | speak | wait
                                          │
                           LLM → Filter/Regen → TTS → transcript/data
```

`turn_lock` vẫn chỉ serialize **LLM generation và speech**. Các loop cập nhật state, goal và environment chạy nền, rẻ và không gọi LLM trừ khi policy cho phép.

### 0.4. Luật thực thi cho AI agent

Trước mỗi task:

1. Đọc `STATE.md`, section liên quan trong `docs/dev_manual/02_modules.md`, và `docs/persona.md` nếu task chạm prompt/filter/LLM.
2. Liệt kê file sửa/tạo và test sẽ viết; chờ user xác nhận nếu task có thay đổi code, DB schema hoặc hành vi live.
3. Dùng interface, YAML config, feature toggle và metric; không hardcode ngưỡng/priority/TTL.
4. Chỉ làm đúng task hiện tại. Không dọn code ngoài phạm vi.

Sau mỗi task:

1. Chạy test task + regression selection. Live test luôn tách marker, không bắt buộc trong unit suite.
2. Báo output, blocker hoặc trade-off; **dừng để user review**.
3. Commit một commit nhỏ: `phaseX: <verb> <scope>`.
4. Chỉ cập nhật `STATE.md` sau khi DoD của task xanh.

---

## 1. Cổng M0 — Production correctness và reproducibility

**Mục tiêu:** trước khi tăng autonomy, đảm bảo hệ thống hiện tại chạy đúng, test được, log đúng và có safety thật trên đường stream.

### M0.1 — Khôi phục môi trường phát triển

- Xác minh Python 3.11+ tồn tại, tạo lại `venv` nếu launcher trỏ đến Python đã mất.
- Pin hoặc lock dependency đã xác minh; loại dependency trùng và ghi rõ extra index cho Windows/CUDA.
- Thêm script PowerShell kiểm tra Python, CUDA, llama-server health, model file và config trước khi live.

**Files dự kiến:** `requirements*.txt` hoặc lockfile đã chọn, `scripts/check_environment.ps1`, `docs/dev_manual/03_operations.md`, test smoke mới nếu phù hợp.

**Tests/DoD:** `python -m pytest tests/` (bỏ 2 test LLM live khi server không chạy) chạy được từ venv mới; script trả lỗi có ích khi thiếu mỗi dependency.

### M0.2 — Wire filter/regenerator vào stream thật

**Vấn đề đã audit:** `RuleFilter` và `FilterRegenerator` có module/test nhưng `build_stream_runtime()` chưa truyền regenerator vào `LLMTurnRunner`.

- Tạo `RuleFilter` từ config/FeatureManager.
- Tạo `FilterRegenerator` với LLM service và config, truyền vào runner.
- Wire filter/regenerator vào dashboard snapshot/metrics nếu dashboard đã hỗ trợ param.
- Fail-open nếu filter lỗi, nhưng ghi metric và `filter_verdict` đúng.

**Files dự kiến:** `orchestrator/stream_runtime.py`, có thể `services/llm/llm_turn.py`, dashboard wiring; không đổi rule filter ngoài task.

**Tests:** integration fake stream runtime; assert output bị filter tạo regen, `last_filter_verdict`/DPO pair hiện diện; feature tắt → backward compatible.

**DoD:** filter config bật thực sự chạy trên luồng `stream_youtube.py`/`stream_discord.py`, không chỉ unit test.

### M0.3 — Identity/session cho dữ liệu

- Mỗi `StreamRuntime`/CLI session sinh UUID ổn định, lưu trong runner.
- `turn_id` có thể local sequence nhưng mọi record và join phải dùng `(session_id, turn_id)`.
- Rating, correction, pref pair, export và dashboard API truyền session id.
- Migration/backward compatibility cho log cũ: record không có session id được gán namespace legacy khi export, không đoán join.

**Files dự kiến:** `services/llm/llm_turn.py`, `orchestrator/stream_runtime.py`, `scripts/cli.py`, `dashboard/dashboard_server.py`, `scripts/export_dataset.py`, tests data/dashboard.

**Tests:** restart fake runner → hai turn id=1 nhưng session khác không overwrite; rating/correction export map đúng bản ghi.

**DoD:** không còn dict join chỉ theo `turn_id` trên data mới.

### M0.4 — Privacy, retention và data integrity

- Chuẩn hóa `schema_version`, timestamp UTC, source và session cho mọi JSONL.
- Mở rộng scrub xuất dataset: tên/handle nhận diện, URL có token, địa chỉ và PII phổ biến; lưu raw log local vẫn phải có retention/consent rõ ràng.
- Dùng hash có salt cục bộ cho viewer identity nếu cần link trong session; không commit salt/map.
- Có lệnh backup/export dry-run; không tự xóa logs.

**Tests:** PII fixture; export giữ join/correction nhưng không còn token/email/phone/ID raw.

**DoD:** dataset có thể chia sẻ nội bộ để train mà không chứa identifier dễ thấy; raw logs có policy rõ.

### M0.5 — CI, docs và smoke live

- GitHub Actions chạy unit/integration không cần llama server; cache deps nếu phù hợp.
- Sửa docs tham chiếu `scripts/stream.py` thành `stream_youtube.py`/`stream_discord.py`; cập nhật số test bằng lệnh thay vì số cứng.
- Thêm smoke command kiểm tra dashboard, YouTube/Discord config (không kết nối thật nếu không có secret), shutdown cancellation.

**DoD M0:** fresh clone có đường setup rõ; test tự động chạy; filter live thật; data không join sai; docs không chỉ entrypoint đã xóa.

---

## 2. Cổng M1 — Agent State và Event Ledger

**Mục tiêu:** tạo trí nhớ làm việc có cấu trúc. Đây là nền của agency, không gọi LLM liên tục.

### Thiết kế tối giản

Tạo `services/agent/`:

```text
types.py          # immutable dataclasses/enums
event_ledger.py   # append + bounded recent grounded events
agent_state.py    # state reducer/snapshot
```

`AgentState` tối thiểu:

```text
current_topic: TopicState | None
open_threads: list[OpenThread]       # max config, TTL
active_goal_ref: str | None          # M2 sở hữu goal
recent_events: list[GroundedEvent]   # bounded
environment_summary: dict | None
stream_phase: opening/main/chat/closing
last_spoken_summary: str | None
```

`GroundedEvent` phải có `source`, `timestamp`, `confidence`, `payload` rút gọn và provenance. Chat text, donation, Director action, speech final, environment observation đều thành event. Không đưa raw log vô hạn vào prompt.

### Atomic tasks

- **M1.1:** dataclass/enums + config limits/TTL + unit reducer test.
- **M1.2:** EventLedger append/prune/dedup; metric event accepted/dropped.
- **M1.3:** ChatRouter, DirectorLoop, self-talk và emotion publish event **sau khi thành công**.
- **M1.4:** Runtime tạo shared AgentState; dashboard snapshot read-only.
- **M1.5:** context renderer chỉ render 3–6 grounded items liên quan; feature toggle mặc định OFF cho rollout an toàn.

**Tests:** clock injection; event out-of-order; cap; duplicate; state snapshot không mutate; source lỗi không giết turn.

**DoD M1:** sau một chat → câu trả lời → follow-up, state mô tả đúng chủ đề và event đã xảy ra, không tự tạo fact mới.

---

## 3. Cổng M2 — GoalManager và Agenda policy

**Mục tiêu:** Mai có một điều đang cố làm. Goal được tạo tự động từ rule/sự kiện thật, nhưng operator luôn override được.

### Model goal

```text
Goal:
  id, kind, status, priority, reason, source
  created_at, expires_at, success_conditions
  suspend_reason, parent_thread_id, metadata (bounded)

status: CANDIDATE | ACTIVE | SUSPENDED | COMPLETED | EXPIRED | CANCELLED
```

Chỉ một `ACTIVE`; số candidate/suspended bị giới hạn trong YAML. P0 donation/safety preempt; goal cũ chỉ resume nếu chưa expired và vẫn relevant.

### Goal kinds MVP

1. `ACK_DONATION` — tự tạo từ superchat, hoàn thành khi ack spoken.
2. `WAIT_FOR_CHAT_ANSWER` — Mai vừa hỏi câu cụ thể; expire nhanh.
3. `CONTINUE_THREAD` — câu chuyện/lời hứa có nguồn thật.
4. `ANSWER_FOLLOW_UP` — viewer trả lời tiếp một open thread.
5. `OPERATOR_PINNED` — dashboard tạo, operator complete/cancel.

Chưa thêm game goal, social goal, narrative arc cho tới M5/M6.

### Atomic tasks

- **M2.1:** Goal types, GoalManager pure state machine, config `agent_goals.yaml`, loader registration, unit tests full lifecycle.
- **M2.2:** Rule-based candidate factories từ donation/question/thread; no LLM planner.
- **M2.3:** Operator API/dashboard: xem active goal, pin/cancel/complete; mọi thao tác ghi event/audit.
- **M2.4:** GoalManager nhận `speech_completed`/chat event để complete hoặc refresh TTL.
- **M2.5:** optional LLM *proposal* API, disabled mặc định; output strict schema và GoalManager validate trước khi accept.

**Metrics:** created/activated/completed/expired/preempted/rejected, age active goal, operator override count.

**DoD M2:** Mai hỏi chat → giữ `WAIT_FOR_CHAT_ANSWER`; donation chen vào → ack → quay lại theo chờ câu trả lời; không có goal stale sau TTL.

---

## 4. Cổng M3 — Director thành Action Arbiter

**Mục tiêu:** giữ Director rule-based, nhưng quyết định hành động theo chat + agenda + environment, không chỉ theo silence.

### Action set theo rollout

MVP thêm dần vào `DirectorAction`, không tạo action chưa có executor:

```text
READ_CHAT, ACK_DONATION, SELF_TALK, FOLLOW_UP, TRANSITION, WAIT   # existing
CONTINUE_THREAD, ASK_FOLLOW_UP, SHARE_GOAL_PROGRESS               # M3
OBSERVE_ENVIRONMENT, EXECUTE_ENV_ACTION                            # M5 only
```

### Atomic tasks

- **M3.1:** `DirectorInput` nhận immutable AgentState/GoalSnapshot; giữ `Director.decide()` pure.
- **M3.2:** rule priority: safety/donation → active goal required action → relevant chat → pacing → self-talk → wait.
- **M3.3:** executors `CONTINUE_THREAD`, `ASK_FOLLOW_UP`, `SHARE_GOAL_PROGRESS`; prompts nhận context cụ thể, không stage prompt trong user message.
- **M3.4:** action completion reports về GoalManager/EventLedger sau speech final.
- **M3.5:** deterministic simulation 1 giờ/1.000 event: no starvation, no infinite resume loop, donation SLA.

**DoD M3:** self-talk không còn tự kích chỉ vì timer khi có active thread/goal hợp lệ; chat unrelated không phá goal có priority cao vô lý; no LLM call ở tick WAIT.

---

## 5. Cổng M4 — Conversation continuity, memory và repair

**Mục tiêu:** Mai giữ mạch, biết điều chưa biết và tự repair thay vì bịa.

### Memory policy

| Lớp | Dữ liệu | Dùng cho |
|---|---|---|
| Working history | 6–12 lượt gần | phản hồi tức thì |
| Open thread | question/promise/story đã xác thực | follow-up và agenda |
| Episodic | event có nguồn/timestamp | “vừa xảy ra” |
| Semantic viewer | fact được xác nhận + TTL | cá nhân hóa nhẹ |
| Narrative | recap có provenance | running gag/câu chuyện |

### Atomic tasks

- **M4.1:** `OpenThreadManager`: create/update/resolve/expire, không LLM.
- **M4.2:** promise/question detector theo rule + LLM extraction optional post-hoc; human-readable evidence bắt buộc.
- **M4.3:** session recap bounded; không đưa full transcript vào prompt.
- **M4.4:** prompt context composer render `current_topic`, open thread, active goal và 3 evidence; context char budget config.
- **M4.5:** conversation repair policy: ambiguity → hỏi lại; missing evidence → nói không chắc; conflicting fact → không khẳng định.
- **M4.6:** regression eval continuity/anti-confabulation fixture từ transcript thật đã sanitize.

**Metrics:** thread open/resolved/expired, promise completion, context chars, repair count, grounded recall rate.

**DoD M4:** eval có các case “nãy cậu bảo…”, “kể tiếp đi”, “ai nói vậy?”; Mai bám evidence hoặc repair, không bịa viewer/event.

---

## 6. Cổng M5 — Mood, persona và proactive hosting

**Mục tiêu:** mood tác động hành vi; autonomy là lựa chọn có lý do, không phải anti-silence filler.

### Atomic tasks

- **M5.1:** mood/flags ảnh hưởng `AgendaPolicy` và Director action score theo YAML, không chỉ prompt style.
- **M5.2:** thay autonomy trigger: candidate từ active goal/open thread/environment, silence pool là fallback cuối.
- **M5.3:** behavior library có cấu trúc: curious, deflect, tease, acknowledge, repair, invite, transition; mỗi behavior có applicability và safety guard.
- **M5.4:** natural timing: pacing/filler chỉ sau khi đo real TTFA; không thêm filler để che lỗi LLM latency.
- **M5.5:** transcript evaluator có rubric: relevance, continuity, persona, non-confabulation, repetition, hostness; operator review 20–30 turn/session.

**DoD M5:** Mai tự nói để tiếp tục thread/goal thật; mood mạnh đổi câu và action hợp lý; force gentle/deflect luôn thắng persona roast.

---

## 7. Cổng M6 — Một environment thật, tool-safe

**Mục tiêu:** Mai có điều để quan sát và hành động ngoài chat. Chỉ chọn **một** adapter đầu tiên.

### Quyết định trước khi code (user chọn)

- `GameEnvironment`: game có log/API đáng tin — khuyến nghị nếu stream game.
- `BrowserEnvironment`: page/video metadata và transcript/controls allow-list.
- `StreamEnvironment`: OBS scene/audio/stream duration — rẻ và ít rủi ro nhất.

Không build vision continuous. Nếu cần vision, phải spike VRAM/latency/prompt-injection trước, chỉ event-driven và structured output.

### Interface tối thiểu

```python
class EnvironmentService(Service):
    async def observe(self) -> EnvironmentState: ...
    def allowed_actions(self) -> list[EnvironmentAction]: ...
    async def execute(self, action: EnvironmentAction) -> ActionResult: ...
```

### Atomic tasks

- **M6.1:** interface/types/config/feature toggle; fake adapter và contract tests.
- **M6.2:** selected adapter observes state, dedups event, writes provenance; read-only.
- **M6.3:** EventLedger/AgentState receives summarized environment facts.
- **M6.4:** Director gains `OBSERVE_ENVIRONMENT`; only speaks when salient.
- **M6.5:** one action allow-list with operator approval mode, audit log and emergency cancellation.

**DoD M6:** Mai bình luận chính xác sự kiện environment thật; không lặp caption; không thực hiện action ngoài allow-list; tắt feature → stream chat vẫn hoạt động.

---

## 8. Cổng M7 — Relationship lite và narrative grounded

**Mục tiêu:** tạo lịch sử xã hội/câu chuyện có thể kiểm chứng, không “fake life”.

### Atomic tasks

- **M7.1:** viewer profile tối thiểu: pseudonymous id, interaction count, confirmed preference, boundary/tone, last seen, TTL.
- **M7.2:** evidence-backed relationship notes: tạo bởi operator hoặc extractor confidence cao; có review/delete trên dashboard.
- **M7.3:** narrative item: event refs, summary, status, expiry; không có evidence → không được inject prompt.
- **M7.4:** running-gag policy: repeated positive interaction + operator review; rate limit reference.
- **M7.5:** privacy export/delete workflow cho viewer records.

**DoD M7:** Mai nhận ra regular qua fact đã xác thực, nhưng không nêu private data/lore bịa; operator xem/xóa mọi note.

---

## 9. Cổng M8 — Eval, data và fine-tune

**Mục tiêu:** mọi thay đổi hành vi được đo trước; fine-tune chỉ dựa trên data sạch và architecture ổn định.

### 9.1. Evaluation harness

Tạo scenario versioned theo nhóm:

```text
director: donation, backlog, cold/hype, starvation
agency: wait answer, resume goal, preemption, expiry
continuity: promise, story, correction, ambiguity
safety: toxic, jailbreak, sensitive user
environment: stale observation, invalid action, action failure
persona: register, mood, gentle override, repetition
```

Mỗi scenario có expected state/action và rubric human-rate, không cố assert exact LLM text. LLM live eval tách marker và lưu artifact sanitized.

### 9.2. Data quality gates

- Dataset chỉ lấy primary output pass filter; correction thay thế SFT target và tạo DPO pair.
- Split theo session, không random split từng turn.
- Báo cáo distribution theo kind, mood, goal, source, operator rating, correction rate, filter hit.
- Quarantine data từ phiên architecture/prompt khác version nếu không compatible.

### 9.3. Fine-tune gate

Chỉ bắt đầu spike SFT khi tất cả đúng:

1. M0–M5 đã ổn định và eval baseline versioned.
2. Có khoảng 2–5k turn tốt; correction có tỷ trọng đủ dùng; DPO pairs chất lượng, không chỉ dedup rác.
3. Persona, mood style, context schema và agenda policy freeze một version.
4. Có holdout session + human eval để chứng minh thắng base model.

SFT trước, DPO sau. Mọi model mới phải rollback được và không thay production model thẳng.

**DoD M8:** model candidate thắng baseline trên holdout theo persona/continuity/repetition mà không làm safety/latency vượt budget.

---

## 10. Cổng M9 — Live operations maturity

**Mục tiêu:** vận hành live lâu, recover được và operator kiểm soát được agent.

### Atomic tasks

- **M9.1:** health supervisor cho llama/TTS/input/dashboard; restart policy rõ, không loop vô hạn.
- **M9.2:** graceful shutdown: cancel task, close websocket, flush logger, save AgentState snapshot; no ASGI traceback.
- **M9.3:** dashboard Agent tab: active goal, open threads, environment state, action queue, audit, controls pin/cancel/pause.
- **M9.4:** emergency stop chặn speech và environment action ngay; resume không resurrect stale goal.
- **M9.5:** soak 2–4 giờ với fake/live controlled inputs, đo memory/queue/latency/errors/data loss.
- **M9.6:** runbook live, incident log và post-stream review/export checklist.

**DoD M9:** live controlled 2 giờ không deadlock/memory leak/data corruption; operator biết Mai đang làm gì và có thể dừng/can thiệp trong một thao tác.

---

## 11. Regression commands và quality gates

Sau **mỗi task code**, chạy selection nhỏ nhất bao phủ task rồi chạy regression liên quan. Ví dụ:

```powershell
# Unit/integration không cần llama-server
python -m pytest tests/ -q --deselect tests/integration/test_llama_server_live.py --deselect tests/integration/test_llm_live.py

# Director/agent sau M1–M3
python -m pytest tests/ -k "director or salience or pulse or agent or goal or thread" -q

# Data/dashboard sau M0/M8
python -m pytest tests/ -k "llm_turn or pref_pairs or export_dataset or dashboard or sanitize" -q
```

Không đánh dấu milestone hoàn thành chỉ vì unit test xanh. Mỗi milestone cần một demo scenario có log/dashboard screenshot hoặc transcript artifact đã sanitize.

## 12. Thứ tự commit khuyến nghị

```text
M0.1 → M0.2 → M0.3 → M0.4 → M0.5
M1.1 → M1.2 → M1.3 → M1.4 → M1.5
M2.1 → M2.2 → M2.3 → M2.4 → M2.5
M3.1 → M3.2 → M3.3 → M3.4 → M3.5
M4 → M5 → M6 (one adapter) → M7 → M8 → M9
```

Mỗi commit chỉ thay một lát dọc có test. Không gộp UI + runtime + schema migration + tuning persona trong cùng commit nếu có thể tách.

## 13. Quyết định cần user trước các cổng lớn

| Trước khi vào | User phải quyết định |
|---|---|
| M2.3 | dashboard được phép tạo/cancel goal nào |
| M5 | mức proactive mong muốn và ranh giới persona khi chủ động |
| M6 | chọn **một** environment đầu tiên và quyền action của Mai |
| M7 | retention, consent và quyền xóa social/viewer data |
| M8 | khi nào freeze input schema để thu data train |
| M9 | recovery có được tự restart service hay chỉ cảnh báo operator |

---

## 14. Định nghĩa “đủ chín”

Mai chỉ được coi là mature host agent khi đồng thời đạt:

- Luồng live an toàn, test/reproduce được, observability và recovery rõ.
- Không còn tự nói chỉ vì im lặng nếu có thread/goal/environment đáng xử lý.
- Có thể bị ngắt bởi donation, nhớ việc đang làm và quay lại đúng lúc.
- Mọi memory/narrative/social fact có provenance hoặc operator review.
- Có ít nhất một environment thật với tool action allow-list.
- Transcript eval chứng minh continuity, naturalness và anti-confabulation tiến bộ qua version.
- Data train được versioned, private-by-default và đủ quality gate trước fine-tune.

**Bước bắt đầu chính thức:** chỉ bắt đầu từ **M0.1**. Không được code GoalManager trước khi M0.2 và M0.3 đã qua DoD, vì agency trên runtime/data không đáng tin sẽ làm lỗi khó audit hơn.
