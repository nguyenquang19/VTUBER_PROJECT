# MAI V2 — MASTER IMPLEMENTATION BLUEPRINT

## Autonomous Conversational Host → World-Aware Autonomous Agent

**Blueprint version:** `2.0`  
**Target product release:** Mai `2.0.0`  
**Current implementation baseline:** Mai `1.4.3`  
**Frozen historical baseline:** Mai `1.0.0`  
**Status:** Canonical migration plan; không phải bằng chứng feature đã production.

> **Trạng thái đối chiếu 19/08/2026:** Phase 1–13 có implementation ở các mức khác nhau nhưng không đồng
> nghĩa production. Director takeover hiện không nắm quyền thật; speech/avatar adapters chưa compose đầy
> đủ; external executor production còn trống; action mock có lỗi thứ tự cập nhật World trước final commit.
> Phase 14–15 là WIP/chưa có release evidence hợp lệ. Trạng thái chi tiết và blockers chỉ lấy từ
> `docs/MAI_V2_SYSTEM_SPEC.md`. Môi trường Python 3.11 đã được phục hồi và cây V1 trùng ở repository root
> đã bị loại; source baseline duy nhất tiếp tục nằm trong `ver/v1.0`. Model/runtime/environment lớn và
> `.env` đã được loại khỏi snapshot theo contract trong System Spec; snapshot hiện source-only. Owner đã
> xác nhận reset Discord credential cũ tại nhà cung cấp. Việc này không thay đổi inventory lịch sử V1.

---

## 0. Mục đích và nguyên tắc sử dụng

Blueprint này hướng dẫn nâng Mai từ runtime AI VTuber hội thoại hiện tại thành autonomous agent có nhận thức tình huống, trạng thái bản thân, capability động, hành động thật và vòng xác minh khép kín.

Blueprint là source of truth cho:

- scope V2;
- thứ tự triển khai;
- điều kiện bắt đầu và hoàn thành từng phase;
- migration gate, shadow gate và release gate.

Code, interface, test, YAML và tài liệu runtime vẫn là source of truth cho behavior đã triển khai. Không được coi nội dung trong blueprint là production trước khi phase tương ứng hoàn tất code, test, replay và release evidence.

Mỗi task chỉ thực hiện đúng một phase. Sau khi chạy test và báo cáo, phải dừng để user review.

---

# 1. Definition of V2

Mai V2 chỉ hoàn thành khi vòng khép kín sau chạy thật:

```text
PERCEIVE
  ↓
WORLD STATE
  ↓
SELF STATE
  ↓
AVAILABLE CAPABILITIES
  ↓
DIRECTOR
  ↓
ACTION REQUEST
  ↓
VALIDATE + RESERVE
  ↓
EXECUTOR
  ↓
ACTION RESULT
  ↓
VERIFY
  ↓
COMMIT / ROLLBACK
  ↓
WORLD UPDATE
  ↺
```

V2 đồng thời phải:

- giữ những behavior tốt của Mai `1.4.3`;
- giảm hành vi giống chatbot;
- không phá persona, continuity và relationship;
- phân biệt hiện tại, ký ức và suy đoán;
- không giả định external action thành công;
- không để LLM tự tạo tool hoặc permission;
- hỗ trợ rollback về decision path cũ trong thời gian migration;
- có metric, health, audit và deterministic replay;
- không chứa logic V3.

Tên “V2”, blueprint version và product version là ba trục độc lập. Product chỉ trở thành `2.0.0` khi release gate cuối cùng đạt và change được chấp nhận phát hành.

---

# 2. Scope khóa cứng

## 2.1. Trong phạm vi V2

- V1 closure và repository hygiene.
- Perception contract và adapter boundary.
- World Model cho current believed state.
- Self Model dạng projection.
- Dynamic Capability Registry.
- Permission và capability availability.
- General `ActionRequest` / `ActionResult`.
- Executor Registry.
- Generic validation, verification và transaction adaptation.
- Director V2 chạy shadow rồi controlled takeover.
- Speech và avatar adapter qua action boundary.
- OBS, media và call executor khi có external integration thật.
- Short intention lifecycle.
- World/event memory và bounded context selection.
- Embodiment Policy.
- Dashboard World/Self/Capabilities/Director.
- Human-like calibration và blind comparison.
- Structured trajectory logging.
- Replay, regression, stress, security và release evidence.

## 2.2. Ngoài phạm vi V2

- Predictive hoặc neural World Model.
- Reinforcement-learning Director.
- MCTS hoặc tree search.
- Long-horizon autonomous planning.
- Online weight update.
- Self-improving production policy.
- Theory-of-Mind engine.
- Microservices rewrite.
- Một model riêng cho từng module.
- LLM tự viết hoặc tự cấp quyền tool.
- Xóa toàn bộ V1 implementation rồi viết lại.
- Bật mọi optional feature chỉ để “đủ V2”.
- Logic V3.

Nếu implementation bắt đầu đi vào nhóm ngoài phạm vi, phải dừng và báo user.

---

# 3. Architectural invariants

1. World Model không chọn action.
2. Self Model không sở hữu bản mutable thứ hai của mood, goal, thread hoặc transaction.
3. LLM không định nghĩa capability khả dụng.
4. Director không gọi external tool trực tiếp.
5. Executor không tự commit business state.
6. Action success không được giả định trước authoritative verification.
7. Lời LLM không phải bằng chứng thế giới đã thay đổi.
8. Memory mô tả quá khứ; World Model mô tả belief hiện tại.
9. EventBus là transport, không phải persistent truth.
10. `StreamRuntime` là composition root, không phải behavior policy.
11. Safety, operator control, permission và transaction conflict thắng soft policy.
12. `WAIT` luôn là một action hợp lệ.
13. Generation success không đồng nghĩa delivery hoặc action success.
14. Mỗi idempotency key không được execute, deliver hoặc commit hai lần.
15. Queue, cache, snapshot và history phải có bound hoặc TTL.
16. Threshold, weight, cooldown, retry và TTL production phải nằm trong YAML.
17. Feature tùy chọn phải đăng ký với `FeatureManager`.
18. Mỗi feature mới phải có metric, health và fail-safe behavior.
19. Không lưu chain-of-thought; chỉ lưu structured reason code và evidence reference.
20. Mỗi phase phải giữ impacted V1 regression xanh.

---

# 4. Những thành phần hiện có phải tái sử dụng

V2 phải mở rộng các boundary sau, không dựng runtime song song:

- `InputEvent`
- `GroundedEvent`
- `EventProvenance`
- `AgentStateSnapshot`
- `GoalManager`
- `OpenThreadManager`
- `ConversationContextComposer`
- `SaliencePool`
- `ChatPulse`
- Mood/Affect và `ResponsePlan`
- Working Memory
- Semantic Memory optional
- Relationship Memory
- EventBus
- Director và `DirectorLoop`
- `ActionTransaction`
- `TTSDeliveryResult`
- `DecisionRecord`
- `LLMTurnRunner`
- `PromptManager`
- Filter/Regenerator
- VieNeu-TTS pipeline
- VTube Studio adapter
- Dashboard infrastructure
- Evaluation/replay/stress infrastructure
- `FeatureManager`
- `ConfigLoader`
- Operations, emergency, shutdown và recovery services

## 4.1. Mapping V1 → V2

| V2 concept | Cách tích hợp |
|---|---|
| `PerceptionEvent` | Boundary DTO mới; adapter từ `InputEvent` hoặc external observation |
| `GroundedEvent` | Event nội bộ đã được xác thực/provenance; không thay thế mù |
| `WorldSnapshot` | Snapshot current belief mới, chạy shadow trước |
| `SelfSnapshot` | Projection từ state/goal/thread/transaction/TTS/avatar/health hiện có |
| `Capability` | Khai báo action mà hệ thống có thể thực hiện |
| `CapabilityAvailability` | Kết quả runtime từ world, self, permission và executor health |
| `ActionRequest` | Lệnh typed đã qua Director nhưng chưa execute |
| `ActionResult` | Kết quả executor; chưa tự động đồng nghĩa verified |
| `ActionTransaction` | Giữ nguyên delivery semantics; thêm compatibility adapter cho action tổng quát |
| `TTSDeliveryResult` | Authoritative result cho speech delivery |
| `DecisionRecord` | Mở rộng schema tương thích để ghi capability/action/verification |
| Director V2 | Mở rộng decision layer hiện có, không tạo composition root thứ hai |

---

# 5. V1 closure gate

Trước feature V2 mới, phải xử lý rõ mọi phần còn sót lại từ Mai `1.4.3`.

“Xử lý” có thể là hoàn tất, giữ optional có chủ đích, thay thế có migration, hoặc loại khỏi V2 blocker. Không bắt buộc biến mọi placeholder thành production.

| Hạng mục còn lại | Quyết định V2 |
|---|---|
| STT/voice input chỉ có interface | Giữ optional; triển khai trong Perception Expansion khi có adapter thật |
| Voice emotion | Phụ thuộc STT; không phải V2 release blocker |
| Environment/game action chưa có executor | Chỉ production khi có typed executor và authoritative verifier |
| OBS/media/call action | Đi qua capability, permission và transaction; không giả lập thành production |
| Semantic memory đang optional | Giữ optional; V2 phải hoạt động đúng khi OFF |
| Hierarchical memory | Không phải V2 blocker; chỉ thêm khi có use case và regression evidence |
| AI filter optional | Rule filter vẫn là production boundary; không bắt buộc bật model filter |
| TTS emotion-aware | Xử lý trong Embodiment phase; không được phá delivery contract |
| Animation micro | Optional; automatic gesture không được xung đột intentional action |
| Agent context toggle | Reconcile với ContextSelector; tránh hai pipeline context song song |
| Goal proposal | LLM chỉ propose; GoalManager vẫn là authority |
| Thread extraction | Optional post-hoc proposal; không tự commit thread |
| Speculative decoding | Performance feature, không thuộc correctness V2 |
| Turn-taking predictor | Optional; `WAIT` và deterministic pacing vẫn phải hoạt động khi OFF |
| Fine-tuned model cutover | Không phải điều kiện kiến trúc V2; llama.cpp production contract giữ nguyên |
| Legacy dashboard | Giữ làm rollback tới khi dashboard V2 đạt parity và migration gate |
| Legacy Director path | Giữ tới khi shadow + controlled takeover + rollback test hoàn tất |
| Bootstrap entrypoint cũ | Tiếp tục fail-fast; không phục hồi pseudo-runtime song song |
| Documentation/version drift | Sửa trước implementation; version trong docs phải khớp YAML |
| Runtime artifact trong source | Không đưa virtual environment, cache, logs, secrets hoặc model vào working tree |

## V1 closure Definition of Done

- Capability inventory được đối chiếu với code và feature YAML.
- Mỗi optional feature có disposition rõ.
- Documentation drift đã được ghi nhận và sửa trong phase phù hợp.
- V1 replay corpus và release evidence được bảo toàn.
- Snapshot V1 không bị sửa.
- Không có runtime artifact được coi là source.
- Không có hai composition root production.
- Baseline human-like và technical metrics được lưu.

---

# 6. Target architecture

```text
REAL / DIGITAL WORLD
        │
        ▼
PERCEPTION ADAPTERS
Chat / Discord / System / OBS / STT / Vision / Game
        │
        ▼
PerceptionEvent
        │ validate + sanitize + dedup
        ▼
GroundedEvent / WorldEvent
        │
        ▼
WORLD MODEL
├ current values
├ confidence
├ provenance
├ authority
├ TTL / staleness
├ conflict resolution
└ bounded snapshots
        │
   ┌────┼───────────────┐
   ▼    ▼               ▼
SELF   MEMORY         AFFECT
MODEL
   └────┼───────────────┘
        ▼
GOALS / SHORT INTENTIONS
        │
        ▼
CAPABILITY REGISTRY
├ declarations
├ permissions
├ executor health
└ runtime availability
        │
        ▼
DIRECTOR
├ HardArbiter
├ CandidateGenerator
├ CandidateScorer
├ SoftPolicy
├ ActionValidator
└ DecisionRecorder
        │
        ▼
ActionRequest
        │
        ▼
VALIDATE → RESERVE
        │
        ▼
EXECUTOR REGISTRY
├ Speech
├ Avatar
├ OBS
├ Media
└ Call
        │
        ▼
ActionResult
        │
        ▼
AUTHORITATIVE VERIFIER
        │
   ┌────┴─────┐
 VERIFIED   FAILED/UNKNOWN
   │            │
 COMMIT       ROLLBACK
   │            │
   └─────┬──────┘
         ▼
WORLD + SELF UPDATE
         ↺
```

---

# 7. Canonical V2 contracts

Contract crossing subsystem phải nằm trong `interfaces/`. Có thể dùng Pydantic frozen model hoặc frozen dataclass theo convention hiện hành.

Mọi timestamp phải là timezone-aware UTC `datetime`.

## 7.1. PerceptionEvent

```python
@dataclass(frozen=True)
class PerceptionEvent:
    schema_version: int
    event_id: str
    source: str
    event_type: str
    timestamp: datetime
    payload: Mapping[str, Any]
    provenance: EventProvenance
    entities: tuple[str, ...] = ()
    confidence: float = 1.0
    dedup_key: str | None = None
```

Rules:

- `event_id`, `source`, `event_type` và provenance bắt buộc.
- `confidence` nằm trong `[0, 1]`.
- Timestamp phải normalize về UTC.
- Payload được validate, sanitize và bound tại adapter.
- Không chứa secret hoặc raw credential.
- Raw viewer identity không được ghi persistent nếu chưa pseudonymize.
- Duplicate không được reduce world state hai lần.

## 7.2. StateValue

```python
@dataclass(frozen=True)
class StateValue:
    value: Any
    source: str
    confidence: float
    updated_at: datetime
    evidence_refs: tuple[str, ...]
    expires_at: datetime | None = None
    authority: int = 0
```

State phải trả lời được:

- giá trị là gì;
- đến từ đâu;
- bằng chứng nào hỗ trợ;
- mức tin cậy;
- còn fresh hay đã stale;
- source nào thắng khi conflict.

## 7.3. WorldSnapshot

```python
@dataclass(frozen=True)
class WorldSnapshot:
    snapshot_id: str
    created_at: datetime
    stream: Mapping[str, StateValue]
    social: Mapping[str, StateValue]
    call: Mapping[str, StateValue]
    media: Mapping[str, StateValue]
    physical: Mapping[str, StateValue]
    game: Mapping[str, StateValue]
```

Không bắt buộc mọi domain production ngay. Domain chưa có adapter thật phải empty hoặc unavailable, không dựng dữ liệu giả.

## 7.4. SelfSnapshot

```python
@dataclass(frozen=True)
class SelfSnapshot:
    snapshot_id: str
    created_at: datetime
    speaking: bool
    busy: bool
    degraded: bool
    current_action_id: str | None
    current_intention_id: str | None
    active_goal_id: str | None
    focused_thread_id: str | None
    current_topic: str | None
    attention_target: str | None
    avatar_state: Mapping[str, Any]
    recent_action_ids: tuple[str, ...]
```

`SelfSnapshot` là projection read-only. Nó không được sở hữu một Mood, Goal hoặc Thread mutable khác.

## 7.5. Capability

```python
@dataclass(frozen=True)
class Capability:
    capability_id: str
    action_type: str
    description: str
    executor_id: str
    verifier_id: str
    risk_level: str
    required_permissions: tuple[str, ...]
    parameter_schema: Mapping[str, Any]
    transaction_policy: str
```

## 7.6. CapabilityAvailability

```python
@dataclass(frozen=True)
class CapabilityAvailability:
    capability_id: str
    available: bool
    reason_code: str
    checked_at: datetime
    evidence_refs: tuple[str, ...]
```

Availability được tính từ:

```text
Capability declaration
+ WorldSnapshot
+ SelfSnapshot
+ permissions
+ executor health
+ verifier health
+ transaction conflicts
```

LLM không tham gia tính availability.

## 7.7. ActionRequest

```python
@dataclass(frozen=True)
class ActionRequest:
    schema_version: int
    action_id: str
    capability_id: str
    action_type: str
    target: str | None
    arguments: Mapping[str, Any]
    intention_id: str | None
    evidence_refs: tuple[str, ...]
    idempotency_key: str
    priority: float
    requested_at: datetime
    transaction_policy: str
```

## 7.8. ActionResult

```python
class ActionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActionResult:
    schema_version: int
    action_id: str
    status: ActionStatus
    started_at: datetime
    completed_at: datetime
    verified: bool
    verification_source: str | None
    result_data: Mapping[str, Any]
    error_code: str | None = None
```

`status=SUCCESS` nhưng `verified=false` không được commit world truth.

## 7.9. DirectorContext

Director chỉ nhận bounded snapshot:

- world snapshot;
- self snapshot;
- available capabilities;
- mood/response plan;
- relevant memory;
- relationship context;
- active goal;
- active short intention;
- open threads;
- recent verified actions;
- chat candidates;
- operator constraints.

Không dump toàn bộ database, transcript hoặc internal state vào prompt.

---

# 8. Configuration, feature và observability policy

Không tạo hàng loạt YAML rỗng từ đầu.

Một YAML mới chỉ được tạo khi:

- có owner subsystem rõ;
- cấu hình không phù hợp với YAML hiện có;
- đã đăng ký trong `ConfigLoader.CONFIG_FILES`;
- có validation type/range;
- có test missing/invalid/reload;
- có documented rollback value.

Feature V2 dự kiến:

- `world_model_shadow`
- `self_model_projection`
- `capability_registry`
- `general_action_transactions`
- `director_v2_shadow`
- `director_v2_takeover`
- `embodiment_policy`
- từng external executor riêng

Tên cuối cùng phải được khóa trong phase tương ứng, không đăng ký trước hàng loạt.

Mỗi phase phải thêm metric ngay khi feature xuất hiện. Phase observability cuối chỉ harden schema và replay, không phải lần đầu thêm metric.

Metric tối thiểu:

- event accepted/rejected/deduplicated;
- state update/stale/conflict/evict;
- capability available/blocked;
- action validated/rejected/executed;
- verification success/failure/unknown;
- transaction commit/rollback;
- executor timeout/degraded;
- shadow agreement/disagreement;
- queue high-water/drop;
- snapshot size và latency.

---

# 9. Coding-agent execution protocol

Trước mỗi phase:

```text
READ
→ blueprint
→ baseline docs
→ module docs
→ interface
→ composition root
→ implementation
→ YAML
→ impacted tests

REPORT
→ phase hiện tại
→ files đã đọc
→ files sẽ tạo/sửa
→ contracts bị ảnh hưởng
→ config/feature/metric
→ tests sẽ thêm/chạy
→ migration và rollback risks

DOCS-FIRST
→ cập nhật canonical docs cho behavior định triển khai

CONFIRM
→ chờ user xác nhận

CODE
→ chỉ phase hiện tại

TEST
→ targeted tests
→ impacted V1 regression
→ replay/stress nếu decision/output thay đổi

REPORT
→ changed files
→ test output
→ metrics
→ known risks
→ rollback path

STOP
```

Cấm:

- làm nhiều phase trong một task;
- đổi public contract không có compatibility/migration test;
- xóa fallback trước shadow validation;
- hardcode tuning number;
- sửa prompt để che state/Director bug;
- thêm one-off rule cho một sample;
- đặt tên test theo phase/milestone;
- tạo test runner song song với `pytest`;
- thay backend llama.cpp;
- tự chuyển phase sau khi test pass.

---

# 10. Implementation phases

## PHASE 0 — Repository stabilization + V1 closure

### Mục tiêu

Khóa trạng thái Mai `1.4.3`, dọn ambiguity và xác định disposition cho mọi phần V1 còn dang dở.

### Công việc

- Xác nhận layout working tree và frozen snapshot.
- Loại runtime artifact khỏi source inventory.
- Đối chiếu version trong config, README, docs và changelog.
- Lập capability matrix production/optional/interface-only/mock.
- Lưu technical baseline, replay baseline và HLC baseline.
- Khóa rollback entrypoint và V1 decision path.
- Ghi disposition cho toàn bộ toggle disabled/optional.

### Test

- documentation guard;
- path/link guard;
- version consistency;
- feature inventory;
- config inventory;
- full offline regression;
- deterministic YouTube replay;
- baseline dashboard/evaluation tests.

### DoD

V1 closure matrix hoàn chỉnh, baseline tái lập được và không còn ambiguity về feature production.

---

## PHASE 1 — Core compatibility contracts

### Mục tiêu

Thêm contract V2 mà không thay behavior.

### Implement

- `PerceptionEvent`
- `StateValue`
- `WorldSnapshot`
- `SelfSnapshot`
- `Capability`
- `CapabilityAvailability`
- `ActionRequest`
- `ActionResult`
- compatibility mapping với contract hiện có

### Test

- validation;
- immutability;
- UTC normalization;
- serialization;
- invalid status/confidence;
- bounded payload;
- backward compatibility;
- no runtime behavior change.

### DoD

Contract pass và Director production chưa thay đổi.

---

## PHASE 2 — World Model shadow

### Implement

- reducer;
- provenance;
- TTL/staleness;
- authority priority;
- conflict resolution;
- bounded snapshot/query;
- state metrics;
- dashboard read-only panel.

### API

```python
apply_event(event)
snapshot()
query(path)
evict_stale(now)
```

### Test

- event → state;
- duplicate;
- TTL;
- conflict;
- source authority;
- uncertainty;
- bounded state;
- invalid event isolation.

### DoD

World snapshot chạy shadow, không tác động production decision.

---

## PHASE 3 — Self Model projection

### Aggregate từ

- `AgentStateSnapshot`;
- Goal/Thread snapshots;
- action transaction;
- TTS speaking state;
- animation state;
- executor/runtime health.

### Test

- immutable projection;
- source changes reflected;
- no duplicate ownership;
- bounded recent action;
- degraded state;
- dashboard snapshot.

### DoD

SelfSnapshot chính xác và không tạo mutable state song song.

---

## PHASE 4 — Capability, permission và health registry

### Capability ban đầu

- `SPEAK`
- `WAIT`
- `READ_CHAT`
- `SELF_TALK`
- `FOLLOW_UP`
- `AVATAR_GESTURE`

Mock-only:

- `PLAY_MUSIC`
- `STOP_MUSIC`
- `SWITCH_SCENE`
- `CALL_GUEST`
- `REMOVE_GUEST`

### Test

- availability theo world/self;
- permission denied;
- unhealthy executor;
- missing verifier;
- transaction conflict;
- unavailable action rejected;
- dashboard AVAILABLE/BLOCKED reason.

### DoD

Registry trả lời được action nào có thể làm ngay bây giờ mà không hỏi LLM.

---

## PHASE 5 — General action mock closed loop

```text
Decision
→ ActionRequest
→ Validate
→ Reserve
→ MockExecutor
→ ActionResult
→ Verify
→ Commit/Rollback
→ World event
```

### Required success scenario

```text
CALL_GUEST(Evil)
→ verified success
→ Evil.connected=true
→ CALL blocked
→ REMOVE available
```

### Required failure scenario

```text
CALL_GUEST(Evil)
→ executor failed hoặc verification unknown
→ rollback
→ Evil.connected vẫn false
→ không tạo success memory
```

### DoD

Mock closed loop end-to-end pass và không phá speech transaction hiện hành.

---

## PHASE 6 — Director V2 shadow

### Components

- HardArbiter
- CandidateGenerator
- CandidateScorer
- SoftPolicy
- ActionValidator
- DecisionRecorder

### Hard priority

- emergency;
- operator;
- safety;
- permission;
- transaction conflict;
- critical state;
- donation theo policy hiện hành.

### Candidate sources

- chat;
- threads;
- goals;
- verified world events;
- available capabilities;
- proactive material;
- `WAIT`.

### Shadow

```text
Current Director → production
Director V2      → propose + structured log only
```

### DoD

Shadow ổn định, bounded, replay deterministic và không đổi live behavior.

---

## PHASE 7 — Controlled conversational takeover

Rollout theo stage:

1. `WAIT`
2. `READ_CHAT`
3. `SELF_TALK`
4. `FOLLOW_UP` và thread actions
5. speech scheduling

Mỗi stage phải:

- có feature flag;
- có rollback switch;
- giữ current Director fallback;
- qua replay, duplicate, cancel và delivery-failure tests.

### DoD

Director V2 sở hữu conversation actions mà không làm tụt V1 regression.

---

## PHASE 8 — Existing speech và avatar executor adaptation

### Speech

Adapter quanh `LLMTurnRunner` và `TTSPipeline`; `TTSDeliveryResult` vẫn là authoritative delivery result.

### Avatar

Adapter quanh VTube Studio; intentional gesture đi qua action system. Mood expression tự động không được giả thành Director action.

### Test

- subtitle degraded success;
- missing delivery callback;
- partial sentence failure;
- cancel;
- duplicate;
- VTS degraded fail-safe;
- no commit before delivery/verification.

### DoD

Speech và avatar dùng action boundary mà vẫn giữ delivery semantics hiện hành.

---

## PHASE 9 — External executor expansion

Thứ tự:

1. OBS
2. Media
3. Call/guest
4. Game/environment khi có integration thật

Mỗi executor cần:

- interface;
- permission;
- health;
- timeout;
- bounded retry;
- idempotency;
- authoritative verifier;
- rollback/failure outcome;
- metric;
- feature flag.

Không có API/event xác minh thì action không được gọi là verified production action.

---

## PHASE 10 — Perception expansion

Adapters:

- Chat compatibility adapter
- System
- OBS
- optional STT
- optional Vision
- optional Game

Cấm:

- SDK callback gọi Director trực tiếp;
- Vision gọi action trực tiếp;
- STT callback execute action;
- raw observation tự ghi world truth.

### DoD

Input mới đi qua cùng canonical boundary và không yêu cầu sửa Director core.

---

## PHASE 11 — Goals và short intentions

Lifecycle:

```text
PROPOSED
ACTIVE
COMPLETED
FAILED
CANCELLED
SUSPENDED
```

Rules:

- chỉ 1–3 logical steps;
- không planning tree;
- LLM có thể propose nhưng không activate/commit;
- action failure phải cập nhật intention theo deterministic policy;
- cleanup có TTL/bound.

### DoD

Director có current intention rõ và không tạo autonomous long-horizon planner.

---

## PHASE 12 — Memory và ContextSelector

ContextSelector chọn bounded context từ:

- World;
- Self;
- Memory;
- Relationship;
- Goal;
- Intention;
- Capabilities;
- Thread;
- Operator constraints.

Rules:

- current truth lấy từ World Model;
- past event lấy từ Memory;
- memory không override fresh world state;
- uncertainty giữ confidence/provenance;
- failed action không được nhớ thành success;
- prompt không dump toàn bộ state.

### DoD

Context giàu hơn nhưng bounded, grounded và không tạo false memory.

---

## PHASE 13 — Embodiment Policy

Ba mức:

```text
LOW
blink / lip-sync / idle
→ automatic

MID
mood / posture / gaze
→ EmbodimentPolicy

HIGH
wave / celebrate / intentional gesture
→ Director Action
```

### Test

- conflict prevention;
- no overlapping intentional actions;
- delivered/verified boundary;
- VTS degraded fail-safe;
- mood không tạo fact hoặc hard priority;
- intentional gesture có evidence/action record.

### DoD

Avatar behavior nhất quán với speech, intention và trạng thái thật.

---

## PHASE 14 — Human-like calibration + observability hardening

### MAI-HLC dimensions

| Dimension | Weight |
|---|---:|
| Language | 20% |
| Presence | 25% |
| Context | 15% |
| Character | 15% |
| Timing | 15% |
| Spontaneity | 10% |

Bổ sung:

- AI Smell yes/no + tag;
- Liveness `/5`;
- action coherence;
- previous-build delta;
- weakest dimension;
- blind A/B.

Blind review phải ẩn:

- build identity;
- Director score;
- prompt;
- memory internals.

Chỉ sau khi lưu score mới cho phép xem internals.

### Trajectory record

```text
S_t
- world_snapshot_id
- self_snapshot_id
- capability_snapshot_id

Decision
- candidate summaries
- selected action
- structured reason_codes

A_t
- ActionRequest

R_t
- ActionResult
- verification outcome

S_t+1
- next snapshots
```

Không lưu chain-of-thought.

### DoD

Có thể replay/debug decision bằng structured evidence và so chất lượng V2 với baseline.

---

## PHASE 15 — Product 2.0.0 release gates

### Gate A — Correctness

- Unauthorized executed action = `0`.
- Unavailable capability executed = `0`.
- Duplicate committed action = `0`.
- False committed world state = `0`.
- Transaction inconsistency = `0`.
- Unbounded state/queue growth = `0`.

### Gate B — Closed-loop agency

Phải chứng minh:

```text
world
→ capability availability
→ decision
→ execute
→ verify
→ commit
→ world changes
→ capability changes
→ next decision sees new state
```

### Gate C — V1 regression

Không phá:

- persona;
- conversation continuity;
- relationship;
- delivery reliability;
- safety;
- dedup/repetition;
- self-talk transaction;
- thread commit semantics;
- graceful shutdown;
- recovery.

### Gate D — Human-like quality

Candidate phải:

- tốt hơn baseline ở aggregate;
- giảm AI Smell;
- không làm metric core collapse;
- không mất Character để đổi lấy naturalness;
- không có blind A/B regression rõ.

### Gate E — Operations và security

- preflight đúng product version;
- release evidence sanitized;
- backup/restore verified;
- permissions deny-by-default;
- no secrets/PII leak;
- emergency stop và graceful shutdown pass;
- rollback path được diễn tập.

### Gate F — Legacy retirement

Chỉ sau khi các gate trên pass mới được:

- đặt `config/system.yaml::app.version` thành `2.0.0`;
- cập nhật `CHANGELOG.md`;
- cập nhật toàn bộ `Applies to`;
- retire Director fallback đã được thay thế;
- retire dashboard legacy nếu parity và recovery đã đạt;
- archive release theo quy trình owner.

Không xóa fallback chỉ vì shadow agreement cao.

---

# 11. V2 Definition of Done

- [ ] V1 closure matrix hoàn tất
- [ ] World state có provenance, confidence và TTL
- [ ] Self state là projection, không duplicate ownership
- [ ] Dynamic capabilities
- [ ] Permission và health-aware availability
- [ ] `WAIT` là viable action
- [ ] Có non-conversation action thật
- [ ] LLM không invent executable capability
- [ ] Validator chặn malformed/unavailable action
- [ ] Typed executor
- [ ] Authoritative verifier
- [ ] Verified success mới commit
- [ ] Failure/unknown rollback
- [ ] Grounded world update
- [ ] Memory không override current truth
- [ ] Dashboard World/Self/Capabilities/Director
- [ ] Human-like calibration
- [ ] Blind build comparison
- [ ] Structured trajectory replay
- [ ] V1 fallback tồn tại đến release gate
- [ ] Replay/regression/stress pass
- [ ] Product version `2.0.0`
- [ ] Không có logic V3

---

# 12. Required acceptance scenario

## Initial state

```text
Chat:
"Sao không gọi Evil vào?"

World:
Evil.available = true
Evil.connected = false

Capabilities:
CALL_GUEST(Evil) = AVAILABLE
REMOVE_GUEST(Evil) = BLOCKED
```

Candidate set:

```text
READ_CHAT
CALL_GUEST(Evil)
SELF_TALK
WAIT
```

## Success path

```text
CALL_GUEST(Evil)
→ Validator PASS
→ Reserve
→ CallExecutor
→ external join confirmation
→ ActionResult SUCCESS + verified
→ Commit
→ World: Evil.connected=true
→ CALL_GUEST blocked
→ REMOVE_GUEST available
→ SelfSnapshot recent action updated
→ next Director tick sees Evil connected
```

## Failure path

```text
CALL_GUEST(Evil)
→ executor FAILED / TIMEOUT / UNKNOWN
→ Rollback
→ Evil.connected remains false
→ no success memory
→ no dialogue pretending Evil joined
→ capability remains consistent with verified state
```

Cả hai path bắt buộc pass.

---

# 13. Locked implementation order

| Order | Phase |
|---:|---|
| 0 | Repository stabilization + V1 closure |
| 1 | Core compatibility contracts |
| 2 | World Model shadow |
| 3 | Self Model projection |
| 4 | Capability, permission và health registry |
| 5 | General action mock closed loop |
| 6 | Director V2 shadow |
| 7 | Controlled conversational takeover |
| 8 | Speech và avatar executor adaptation |
| 9 | External executor expansion |
| 10 | Perception expansion |
| 11 | Goals và short intentions |
| 12 | Memory và ContextSelector |
| 13 | Embodiment Policy |
| 14 | Human-like calibration + observability hardening |
| 15 | Product `2.0.0` release gates |

Không được đổi thứ tự hoặc gộp phase nếu chưa có user approval.

---

# 14. Coding-agent starter prompt

```text
You are implementing Mai V2 from
MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md.

Work on exactly ONE phase.

Before editing:
1. Read the blueprint and identify the current phase.
2. Read the frozen baseline documentation for regression context.
3. Read current runtime docs, interfaces, composition root, implementation,
   YAML and impacted tests.
4. Report:
   - current phase
   - files read
   - files to create/modify
   - contracts affected
   - config and feature flags
   - metrics
   - tests to add/run
   - migration and rollback risks
5. Update canonical documentation first.
6. Wait for user confirmation before implementation.

Implementation rules:
- Preserve working behavior unless the phase explicitly migrates it.
- Preserve fallback until shadow and rollout gates pass.
- Do not modify the frozen V1 snapshot.
- Do not add V3 features.
- WorldModel never decides actions.
- SelfModel never duplicates mutable Goal/Mood/Thread ownership.
- LLM never defines capabilities or permissions.
- Director never executes external tools directly.
- Executor never commits business state directly.
- No state commit before authoritative verification.
- Every optional feature needs FeatureManager registration.
- Every feature needs metrics, health, tests and fail-safe behavior.
- No production threshold, TTL, cooldown, retry or weight in Python.
- Do not persist chain-of-thought.
- Do not fix isolated dialogue samples with one-off rules.
- Use PowerShell on Windows.
- Use llama.cpp as the only LLM backend.

After coding:
1. Run targeted tests.
2. Run impacted V1 regression tests.
3. Run deterministic replay when decision/output changes.
4. Report:
   - changed files
   - test output
   - metrics
   - known risks
   - rollback path
5. STOP. Do not continue to the next phase.

Current task:
PHASE <N> — <NAME>
```

---

# 15. Documentation conflict rule

Nếu blueprint, docs và runtime mâu thuẫn:

1. Không tự đoán.
2. Không sửa frozen baseline để hợp thức hóa behavior mới.
3. Xác minh theo thứ tự source of truth của repository:
   - interface/model;
   - runtime composition;
   - implementation;
   - YAML;
   - tests;
   - runtime documentation.
4. Báo conflict trước khi sửa.
5. Cập nhật docs-first trong phase phù hợp.
6. Không xóa hoặc di chuyển code dựa trên instruction stale.
7. Blueprint chỉ khóa scope/order; nó không chứng minh feature đã production.

---

# 16. Final success principle

V2 không phải “thêm nhiều AI hơn”.

V2 thành công khi Mai:

- hiểu tình huống tốt hơn;
- biết trạng thái hiện tại của thế giới;
- biết bản thân đang làm gì;
- biết capability nào thực sự khả dụng;
- chọn hành động tự nhiên và an toàn;
- thực hiện hành động qua typed executor;
- biết kết quả thật qua verification;
- chỉ commit điều đã xảy ra;
- tiếp tục hội thoại nhất quán;
- nghe bớt giống chatbot;
- vẫn giữ được cá tính của Mai;
- có thể đo, debug, replay và rollback toàn bộ vòng hành động.
