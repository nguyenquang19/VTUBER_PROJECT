# MAI V2 — MASTER IMPLEMENTATION BLUEPRINT

## Autonomous Conversational Host → World-Aware Autonomous Agent

**Blueprint version:** `2.0`  
**Target product release:** Mai `2.0.0`  
**Current implementation baseline:** Mai `1.4.3`  
**Frozen historical baseline:** Mai `1.0.0`  
**Status:** Canonical migration plan; không phải bằng chứng feature đã production.

> **Trạng thái implementation không được nhân bản trong blueprint.** Closure phase, test evidence,
> production composition, feature state và blockers hiện tại chỉ lấy từ
> `docs/MAI_V2_SYSTEM_SPEC.md`. Blueprint này chỉ khóa scope, thứ tự và acceptance gate để tránh một ảnh
> chụp tiến độ cũ bị hiểu nhầm thành behavior production.

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

## 7.10. Cognitive Brain contract migration

MCB là migration track đã được owner chấp nhận để thay dần quyền quyết định mềm của policy hội thoại bằng
một Brain dùng LLM, nhưng không thay kernel đã kiểm chứng. Track này không đổi định nghĩa product V2, không
đổi thứ tự hay trạng thái closure của Phase 0–15 và không tự chứng minh capability đã production.

Ranh giới quyền lực khóa cứng:

- Brain sở hữu đề xuất attention, intent, `WAIT`/speech và wording trong scope được rollout;
- kernel sở hữu truth, provenance, freshness, capability, permission, emergency/operator hold, transaction,
  execution, verification, delivery và mọi domain commit;
- Brain chỉ trả proposal bất biến; không tạo capability, không gọi executor, không ghi World/Self/Goal/
  Focus/Memory và không tuyên bố external success;
- `CognitiveActionProposal` phải tham chiếu capability/action envelope hiện có. Chỉ kernel mới được
  materialize nó thành `ActionRequest` sau validation;
- `MemoryProposal` không phải `MemoryEntry`; success memory chỉ được materialize sau verified outcome;
- `FocusState` là continuity ngắn hạn, không phải World truth hoặc Goal và chỉ được commit tại boundary do
  kernel sở hữu;
- soft style chỉ được quan sát; hard safety, privacy, grounding và malformed output vẫn fail closed;
- feature-off hoặc Brain chưa khả dụng giữ exact compatibility path.

Crossing-subsystem contract của track này nằm trong `interfaces/cognition.py` và dùng các tên canonical:
`CognitionConfig`, `CognitiveContext`, `CognitiveHardState`, `CognitiveEvidenceSource`,
`CognitiveEvidenceItem`, `CognitiveConversationState`,
`CognitiveMemoryItem`, `CognitiveSpeechSummary`, `CognitiveActionEnvelope`, `CognitiveTurn`,
`CognitiveMode`, `CognitiveUncertainty`, `FocusState`, `FocusOrigin`, `FocusOperation`, `FocusProposal`, `MemoryKind`,
`MemoryScope`, `MemoryClaimBasis`, `MemoryRetentionClass`, `MemoryProposal`, `CognitiveActionProposal` và
`CognitiveBrainService`. MCB-1 chỉ được tạo contract/config/feature boundary bất hoạt; không được có
llama.cpp call, scheduler, Director takeover, delivery, action execution hoặc state mutation.

MCB-2 chỉ dựng lớp chuyển đổi typed, deterministic từ các owner hiện hữu sang `CognitiveContext` và một
`FocusState` shadow dẫn xuất. Contract bổ sung canonical là `CognitiveContextRequest` và
`CognitiveContextBuilderService`; service trả `CognitiveContext | None`, giữ bounded in-memory snapshots cho
diagnostic nhưng không persist, không được ghi ngược vào World/Self/Goal/Thread/Memory và không được nhận hay
commit `FocusProposal`. `None` nghĩa là required source không thể tạo context an toàn; slice có Brain sau này
phải đi exact compatibility path.

Source authority MCB-2 khóa như sau:

- `WorldModelService`, `SelfModelService`, `CapabilityRegistryService`, `AgentStateService`,
  `GoalManagerService`, `OpenThreadManagerService` và `MemoryService` vẫn là owner; Context Builder chỉ đọc
  public snapshot/query contract;
- hard state do kernel tạo tại opportunity boundary; mọi hard hold co `available_modes` về đúng `WAIT`;
- chat chỉ được chọn bằng exact `trigger_event_ref`, không match gần đúng; recent speech/Focus claim chỉ nhận
  `SPEECH_COMPLETED` authoritative;
- Focus chỉ materialize khi `SelfSnapshot.focused_thread_id` khớp một open thread còn fresh. Pressure và
  saturation là projection deterministic theo YAML, không phải truth hoặc state mới;
- memory thiếu scope/kind/provenance/confidence rõ ràng bị omit; viewer scope không được broaden và World/Self
  hiện tại tiếp tục thắng memory khi conflict;
- capability snapshot được dùng cho identity/freshness nhưng MCB-2 chưa cấp `PROPOSE_ACTION`; action envelope
  để rỗng tới slice MCB-8;
- context ID là SHA-256 của canonical JSON sau selection/order/bounds, loại chính field `context_id`; timestamp
  build lấy từ request để same-input replay không phụ thuộc wall clock.

MCB-2 không compose consumer vào `StreamRuntime`/`DirectorLoop`, không tạo background task, không gọi
llama.cpp/tokenizer, không đổi prompt/decision/output, không thêm mutable Focus owner và không mở khóa
`cognitive_brain_shadow`. MCB-3 mới được docs-first về opportunity, queue/timeout, tokenizer preflight và
Brain shadow consumer.

MCB-3 chỉ được thêm một Brain observer dùng llama.cpp; compatibility Director vẫn quyết định và chạy ngay,
không chờ Brain. Heartbeat 1,5 giây không phải Brain opportunity. Một opportunity chỉ được mở từ thay đổi
authoritative có ý nghĩa: chat/donation/operator input mới, verified outcome làm đổi lựa chọn kế tiếp,
Focus/proactive readiness đến hạn hoặc Goal/Thread cần conversational move. Hard hold, transaction không
tương thích, feature/service không sẵn sàng, duplicate trong debounce, context không vừa token budget và
shutdown đều chặn call trước llama.cpp.

Brain shadow dùng bounded latest-wins queue, tối đa một generation đang chạy và workload class thấp hơn live
speech. Live generation phải preempt/cancel shadow; shadow không được giữ `turn_lock`, không được làm
Director chờ và không được chạy đồng thời có chủ ý với live generation. Context được token-count bằng exact
llama.cpp chat template và reject whole request khi vượt budget; không compact âm thầm một structured prompt.
Output dùng llama.cpp JSON Schema, chỉ có subset `WAIT | SPEAK` của `CognitiveTurn`; MCB-3 cấm action,
Focus và Memory proposal. Parser reject markdown/trailing text, duplicate/unknown key, invalid enum/bound/
reference hoặc stale context; không retry/regenerate trong cùng opportunity và không yêu cầu/lưu chain-of-thought.

Observer record được giữ bounded in-memory để so mode/latency với compatibility decision nhưng không được
gọi transaction, TTS, delivery, executor hoặc domain write. Feature-off, skip, timeout, cancel, parse/schema
failure và stale output đều là no-op đối với public path: exact compatibility result đã chọn vẫn giữ nguyên.
MCB-3 có thể cho phép bật riêng shadow qua `cognitive_brain_shadow`, nhưng mặc định tiếp tục tắt và không cấp
takeover authority. Sáu acceptance threshold về schema failure, timeout, call ratio, event-to-first-audio,
queue wait và primary fallback phải được owner duyệt trước code/activation; giá trị thiếu baseline không được
agent tự biến thành production gate.

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

## 13.1. Locked MCB migration track

MCB bắt đầu từ comparison baseline kỹ thuật `ea4910bd9e7c53af16131f51c542445a8bcdee24`. Owner đã chấp nhận
baseline kỹ thuật này, duyệt và triển khai MCB-1 contract-only ngày 23/08/2026. Blind MAI-HLC 20 pair đã persist/finalize
hợp lệ nhưng quality vẫn `HOLD`: candidate tăng aggregate/Presence/Character nhưng Context giảm và
AI-smell không giảm. Quality HOLD không cho phép suy ra cải thiện chất lượng và tiếp tục chặn offline
quality acceptance, takeover, canary hoặc release cho tới khi một candidate sau đạt quality gate đã khóa.

| Order | Slice | Quyền được thêm | Gate chính |
|---:|---|---|---|
| MCB-0 | Baseline audit/rework | Không có runtime authority mới | Clean source-bound technical baseline; human quality được báo riêng |
| MCB-1 | Contract foundation | Contract/config/disabled feature only | Immutable strict shape, feature-off, no LLM/no mutation |
| MCB-2 | Context + Focus shadow | Read-only context/projection | Deterministic ID/order/bounds/freshness; no domain commit |
| MCB-3 | Brain shadow | Proposal observation only | llama.cpp schema/latency/opportunity gates; no delivery/action/state side effect |
| MCB-4 | Offline A/B | Không có live authority | Same-input comparison và ít nhất 30 blind discovery pairs |
| MCB-5 | READ_CHAT takeover | `WAIT`/`READ_CHAT` trong accepted envelope | Limited canary, exact rollback và ít nhất 60 blind pairs |
| MCB-6 | SELF_TALK/FOLLOW_UP takeover | Bounded conversational scope | Focus commit only after delivery; interrupt/recovery evidence |
| MCB-7 | Memory/carryover | Proposal only; kernel commits | Provenance/privacy/outcome/TTL/idempotency gates |
| MCB-8 | Embodiment/action proposal | Proposal trong current capability envelope | Permission/health/verification/rollback; zero false success |
| MCB-9 | Model bake-off/fine-tune | Không đổi kernel authority | Frozen contract/corpus, sanitized split và same-harness A/B |
| MCB-10 | Release | Chỉ scope đã canary/review | Same clean revision, 100 blind pairs, operations/security/rollback và owner approval |

MCB-1 non-goals được khóa: không context builder runtime, không Brain adapter/prompt, không scheduler,
không Director/TTS/action/memory/Focus takeover hoặc mutation, không đổi model/sampling/persona, không xóa
compatibility component và không tăng product version. Sau mỗi slice phải dừng để owner review; không tự
chuyển sang slice kế tiếp.

Owner đã duyệt docs-first MCB-2 và implementation read-only đạt ngày 24/08/2026: typed request, Context
Builder, Focus projection, strict cognition config, bounded metrics/tests đã có nhưng chưa compose consumer
vào runtime. Slice không sửa decision path, không gọi LLM, không persist context/Focus, không nhận proposal
và không tự chuyển MCB-3.

MCB-2 implementation đã được chốt tại commit `75814e9`; docs-first MCB-3 được chốt tại `c0ae3c2`; MCB-3
implementation được chốt tại `d6982b9`. Brain observer, structured llama.cpp boundary, opportunity scheduler,
resource preemption, validation, metrics và exact compatibility semantics đã có. Feature vẫn mặc định tắt,
chưa có quyền takeover và real llama.cpp baseline/sáu numerical acceptance gate vẫn `HOLD` theo protocol đã
duyệt trong System Spec.

Owner đã cho phép bắt đầu docs-first MCB-4 ngày 24/08/2026. Slice này chỉ được thêm một harness A/B ngoại
tuyến, source-bound và persist-before-reveal; không bật Brain trong live runtime, không đọc kết quả Brain vào
Director và không thay đổi delivery/action/state authority. Mỗi pair phải bind cùng corpus case, authoritative
pre-turn snapshot, persona/lore profile, model identity và deterministic generation seed. Khác biệt adapter/
prompt giữa compatibility path và Brain là biến kiến trúc được đo, phải được sealed manifest ghi nhận và
không được che thành “cùng prompt”.

MCB-4 phải báo riêng: ma trận compatibility action/`WAIT` so với Brain `SPEAK`/`WAIT`; schema/timeout/stale/
preflight exclusions; coverage natural và adversarial; ít nhất 30 informative blind discovery pairs; MAI-HLC
dimensions, AI-smell, liveness và action coherence sau reveal. Pair selection phải deterministic, stratified
và công bố cả excluded denominator; không được chỉ chọn các lượt mà cả hai path đều nói hay. Cặp có đúng một
path `WAIT` dùng marker hiển thị canonical trong review; cặp cả hai `WAIT` chỉ vào decision matrix, không được
độn số pair chất lượng. Automated precheck và aggregate không tự tạo go/no-go. MCB-3 gate còn `HOLD` hoặc
human review chưa hoàn tất đều chặn MCB-5; chỉ owner mới quyết định controlled takeover.

Code MCB-4 trong working tree đã có cả artifact harness và source producer ngoại tuyến. Corpus strict có 40
case thuộc 8 strata, gồm evidence fresh/stale/missing/malformed để vẫn còn đủ 30 pair sau exclusion. Producer
chạy compatibility qua Director/PromptManager/configured hard filter và Brain qua chính
`CognitiveContextBuilder`/`CognitiveBrain`; cùng case/seed/sampling, hoán đổi thứ tự deterministic, không canned,
regeneration, delivery hoặc state write. CLI có thể collect source rồi nối thẳng build/finalize artifact chain;
full offline `2.448 passed`. Dirty-source llama.cpp diagnostic cuối chạy đủ 40 case nhưng chỉ có 18 informative
pair: Brain 20 completed, 7 timeout, 5 schema reject, 5 parse reject, 2 preflight reject và 1 stale. Harness
đã fail-closed trước blind build. Clean-SHA evidence, 30 persisted human score và reveal vẫn chưa có, nên
MCB-4 tiếp tục `HOLD`; phải rework MCB-3 reliability riêng trước khi MCB-4 có thể đạt gate.

Rework này là một lát căn chỉnh MCB-1→MCB-4, không phải MCB-5 và không mở authority mới. Typed
`CognitiveContext`/`CognitiveTurn` vẫn là kernel contract; Brain adapter chỉ serialize decision-view gồm toàn
bộ item đã được Context Builder chọn nhưng bỏ metadata vận chuyển không giúp quyết định. JSON Schema phải
encode cùng invariant `WAIT`/`SPEAK` mà `CognitiveTurn` validator đang enforce; surrounding whitespace được
phép, duplicate array member được order-preserving deduplicate vì llama.cpp hiện không enforce
`uniqueItems`, nhưng duplicate object key, markdown/prefix/suffix/unknown key vẫn fail-closed.
Output/intent/speech budget và
timeout/opportunity-age phải đồng bộ với latency/token evidence thật, không tiếp tục suy từ MCB-0B public
turn vốn có wire shape khác. A/B producer dùng đúng các bound mới cho cả hai candidate, chạy lại toàn bộ 40
case và vẫn phải đạt ít nhất 30 informative pair trước khi tạo blind artifact. Không được sửa persona để
chữa từng sample, không thêm retry/fallback và không dùng kết quả dirty-source làm gate.

Kết quả implementation/replay của lát căn chỉnh: full offline `2.453 passed`; dirty-source llama.cpp run v3
có 40 case, compatibility `40/40 COMPLETED`, Brain `37/40 COMPLETED`, 2 preflight reject chủ đích và 1 stale
chủ đích, không còn timeout/parse/schema reject. Có 35 informative pair và harness đã persist đúng 30 pair
blind cùng sealed manifest; zero delivery/state mutation. Đây là closure kỹ thuật của source producer, nhưng
không phải MCB-4 acceptance vì còn thiếu clean exact-SHA replay và owner human review/reveal. MCB-4 vẫn
`HOLD`, MCB-5 vẫn chưa được bắt đầu.

Owner yêu cầu rework blind corpus vì các case độc lập chưa phản ánh khả năng theo mạch của Mai. MCB-4 vì vậy
được phép thay corpus bằng 8 mini-arc × 5 beat, nhưng không được biến evaluation thành closed-loop simulation:
mọi beat dùng một canonical prior transcript chung, không dùng output của candidate trước làm input beat sau.
Corpus phải bind `arc_id/title`, exact `turn_index/arc_length`, bounded typed prior turns và cùng authoritative
OpenThread/recent-delivery snapshot cho hai path. Blind selection vẫn stratified nhưng sau khi chọn phải nhóm
theo canonical arc order + turn order; A/B role vẫn swap độc lập từng pair. Gate bổ sung là mỗi required arc
có ít nhất số pair do YAML quy định, context review hiển thị đủ episode/beat/prior transcript/current chat,
và manifest tiếp tục che role/build/prompt. Rework vẫn là MCB-4 offline, không mở Brain authority hoặc MCB-5.

Implementation story-arc đã thay corpus rời bằng `cognitive_ab_story_v2.yaml`: 40 case thuộc đúng 8 arc × 5
beat, giữ 8 strata kỹ thuật và materialize canonical prior transcript thành grounded events, verified recent
speech cùng `OpenThread` dùng chung. Blind selection dành trước tối thiểu 2 pair cho mỗi arc rồi mới fill theo
stratum, sau đó sắp đúng arc/turn order. Dirty-source llama.cpp diagnostic story v1 đạt compatibility `40/40
COMPLETED`, Brain `36/40 COMPLETED`, 3 preflight reject + 1 stale chủ đích, không timeout/parse/schema reject;
có 33 informative pair và persist đúng 30 blind pair, phủ cả 8 arc từ 2–5 pair/arc, zero delivery/state
mutation. Đây chỉ xác nhận fidelity và độ đủ của bộ review mới; chưa chứng minh Brain nói người hơn vì artifact
vẫn `pending_human_review` và source không sạch. Targeted story/evaluation đạt `68 passed`, impacted đạt `381
passed` và full offline đạt `2.454 passed`. MCB-4 tiếp tục `HOLD`, MCB-5 chưa được bắt đầu.

Owner đã hoàn tất và finalize blind story-arc review. Sau reveal, compatibility thắng `11`, Brain thắng `9`
và có `10` tie; weighted score chỉ dịch từ `3.5833` lên `3.5967` (`+0.0134`), AI-smell giữ nguyên
`33.33%`. Brain tăng Character nhưng giảm Timing, Spontaneity và Liveness; đây là tín hiệu kiến trúc chứ
không phải bằng chứng candidate tổng thể tốt hơn. Artifact vẫn từ dirty source nên không có gate authority.

Owner sau đó yêu cầu dừng việc bồi thêm tầng và audit toàn dự án để chọn một đường duy nhất. Target được chọn
là **single-generation Brain-on-Kernel**: một llama.cpp Brain là soft decision owner và đồng thời tạo exact
`CognitiveTurn`; kernel chỉ sở hữu hard precedence, capability/permission, transaction, output validation,
delivery verification và state commit. Không thêm decision/voice generation thứ hai. Kernel không được dùng
rule/template để viết lại câu, clamp số từ/câu hỏi hoặc regenerate style; hard-invalid output bị reject và
fallback duy nhất trước takeover đầy đủ là `WAIT`, không phải một conversational policy song song.

Đường đích duy nhất là:

```text
canonical event -> authoritative state -> kernel preflight -> one Brain generation
-> strict CognitiveTurn validation -> transaction/delivery -> verified state/Focus/Memory commit
```

Migration phải loại dần, không duy trì vĩnh viễn, các lớp trùng owner: legacy Director, Director V2 shadow/
takeover/materializer, Cognitive observer shadow wrapper, Autonomy/SelfTalk planning song song, text context
renderer song song với `CognitiveContext`, style regeneration/clamping và mock-only runtime services. Chỉ được
xóa từng owner sau khi đường Brain tương ứng đã có exact-live shadow evidence, controlled takeover, rollback
artifact và owner approval; trước đó component cũ là rollback tạm thời chứ không phải kiến trúc đích.

MCB-4 được thu hẹp thành nơi sửa fidelity phép đo: candidate cũ phải là exact live-equivalent public path,
candidate mới là single-generation Brain dùng cùng canonical pre-turn state; không dùng harness giản lược để
kết luận production. MCB-5/6/7/8 lần lượt chuyển speech scheduling, conversational continuity, memory và
action proposal sang đường duy nhất này. MCB-10 mới được xóa toàn bộ compatibility/migration scaffolding đã
được thay thế. Kết quả blind cũ chỉ là baseline chẩn đoán; MCB-4 tiếp tục `HOLD`, MCB-5 chưa được bắt đầu.

## 13.1. Kế hoạch chuẩn hóa toàn bộ cấu trúc Brain-on-Kernel

Mục tiêu của kế hoạch này là biến working tree hiện tại thành một cấu trúc source/runtime duy nhất, có ranh
giới package rõ, dependency một chiều và không còn nhiều owner cho cùng một quyết định. Đây là kế hoạch
**tái cấu trúc hệ thống**, không phải roadmap thêm feature. MCB chỉ cung cấp gate để chuyển dần behavior đang
chạy; cấu trúc đích dưới đây mới là thứ dẫn dắt mọi thao tác move/merge/delete.

### 13.1.1. Cấu trúc repository đích

```text
v2.0/
├── interfaces/                 # DTO/Protocol thuần, không import implementation
│   ├── events.py               # CanonicalEvent + provenance
│   ├── state.py                # Authoritative snapshots/proposals
│   ├── cognition.py            # CognitiveContext/CognitiveTurn/Brain contract
│   ├── execution.py            # Action/Delivery/Verification contracts
│   └── operations.py           # Health/metric/control contracts
├── orchestrator/               # Chỉ composition, lifecycle và entrypoint
│   ├── stream_runtime.py       # Dựng graph, start/stop; không chứa behavior policy
│   ├── runtime_config.py       # Load/validate config canonical
│   └── entrypoints/            # YouTube, Discord và combined launch
├── services/
│   ├── ingress/                # YouTube/Discord/System/OBS adapters -> CanonicalEvent
│   ├── state/                  # Reducer + World/Self/Thread/Focus/Goal/Memory/Affect owners
│   ├── cognition/              # ContextBuilder + one-generation Brain + prompt/model adapter
│   ├── kernel/                 # Scheduler, preflight, validator, transaction, outcome commit
│   ├── execution/              # Speech/TTS/subtitle/avatar/OBS/external executors + verifiers
│   └── operations/             # Metrics, journal, health, emergency, control plane, dashboard
├── services/evaluation/        # Library offline; không được compose vào live runtime
├── eval/corpora/               # Corpus/data versioned, sanitized
├── scripts/                    # CLI launch/replay/evaluation/maintenance
├── config/                     # Một owner YAML cho mỗi domain
│   ├── system.yaml
│   ├── model.yaml
│   ├── cognition.yaml
│   ├── state.yaml
│   ├── execution.yaml
│   ├── operations.yaml
│   └── evaluation.yaml
└── tests/
    ├── unit/                   # Mirror package owner
    ├── integration/            # Vertical Brain->Kernel->Delivery->Commit slices
    ├── regression/             # Compatibility/release behavior
    └── acceptance/             # Replay, canary, soak và release gate
```

Tên file cuối có thể được điều chỉnh ở docs-first của lát cắt, nhưng không được tạo thêm top-level runtime
package hoặc một composition root thứ hai. `services/evaluation` và `scripts` có thể import public contract/
adapter của live; live runtime không được import evaluation harness, corpus hoặc release tooling.

### 13.1.2. Luật dependency và ownership

```text
entrypoint -> orchestrator -> services -> interfaces
                              |
                              +-> external libraries/devices

evaluation -> public live contracts/adapters
live runtime -X-> evaluation/corpus/release tooling
domain service -X-> orchestrator
interface -X-> service implementation
```

| Concern | Owner cuối | Phần phải bị gộp/xóa |
|---|---|---|
| Chuẩn hóa sự kiện | `services/ingress` | Adapter tự dựng state hoặc gọi Director |
| Sự thật hiện tại | `services/state` | Bản mutable thứ hai trong Director/context/dashboard |
| Context quyết định | `CognitiveContextBuilder` | AgentContextRenderer và context composer song song |
| Quyết định mềm + exact speech | `CognitiveBrainService` | Legacy Director/V2 soft scorer, behavior/style writer |
| Hard gate | Turn Kernel | Hard rule nằm trong prompt hoặc output rewriter |
| Side effect | `services/execution` | Director/Brain gọi thiết bị trực tiếp |
| Success/failure | Verifier | Model hoặc executor tự tuyên bố success |
| State/history commit | Outcome Committer | Pre-delivery commit và writer rải rác |
| Quan sát/vận hành | `services/operations` | Metric/dashboard trở thành policy |
| Đánh giá | `services/evaluation` | Harness giản lược hoặc được compose live |

Mỗi concern chỉ có một mutable owner. Service khác chỉ nhận immutable snapshot hoặc typed proposal. Brain
được tạo `WAIT | SPEAK | PROPOSE_ACTION` và exact `speech_text` trong một generation, nhưng không có quyền
permission, execute, verify hoặc commit.

### 13.1.3. Chín wave chuẩn hóa cấu trúc

| Wave | Thay đổi cấu trúc | Kết quả bắt buộc | Chưa được làm |
|---|---|---|---|
| S0 — Inventory/freeze | Lập import graph từ entrypoint; gắn mọi file/config/feature/metric/test vào `KEEP/MERGE/DELETE/OFFLINE/ARCHIVE` | Có replacement owner và deletion gate cho từng phần dư thừa; clean checkpoint tái lập được | Move/delete hoặc đổi behavior |
| S1 — Interface boundary | Gom DTO/Protocol crossing subsystem vào `interfaces`; loại import implementation khỏi contract | Import-boundary tests; không cycle; shape hiện tại vẫn tương thích | Thay live owner |
| S2 — State + ingress | Gom adapter vào ingress; gom reducer/store/projection theo một authoritative state layer | CanonicalEvent đi vào một reducer; dashboard/context chỉ đọc snapshot | Brain/Director ghi state trực tiếp |
| S3 — Cognition | Giữ một ContextBuilder, một prompt/model adapter và một Brain service; offline dùng cùng adapter | Cùng pre-turn state tạo cùng context identity; một generation tạo CognitiveTurn | Delivery/action/state mutation |
| S4 — Turn Kernel | Tách execution shell/hard arbiter/transaction khỏi DirectorLoop; hợp nhất Brain scheduler | Kernel chỉ opportunity/preflight/validate/route; đúng một public owner mỗi lượt | Kernel viết lại câu hoặc fallback conversational |
| S5 — Execution + outcome | Gom TTS/subtitle/avatar/OBS/action vào typed executor/verifier; gom writer thành Outcome Committer | `reserve -> execute/deliver -> verify -> commit/release`; verified history feedback | Commit success trước verifier |
| S6 — Continuity | Đưa Thread/Focus/recent speech/Memory vào state layer và proposal/commit flow duy nhất | Lượt sau đọc exact verified outcome lượt trước; TTL/privacy/idempotency có owner | Planner/context writer song song |
| S7 — Operations + offline split | Hợp nhất journal/metric/health/control/dashboard; tách eval/canary/soak/release khỏi live graph | Một lineage bounded; live dependency graph không chứa offline tooling | Operations chọn soft action |
| S8 — Delete + compact | Xóa wrapper, re-export, toggle, config, metric, dashboard panel và test superseded theo dependency closure | Zero live importer của owner cũ; StreamRuntime chỉ composition/lifecycle; config/test tree khớp package đích | Giữ dead path “để phòng” sau release |

Wave phải chạy theo thứ tự. Trong S1–S7 có thể giữ compatibility import adapter tạm thời, nhưng adapter phải
có owner, removal wave và test chứng minh exact behavior. Không tạo wrapper không có hạn xóa.

### 13.1.4. Cách xử lý source hiện tại

| Nhóm hiện tại | Disposition đích |
|---|---|
| YouTube/Discord/chat router/perception adapters | `MERGE` vào ingress canonical |
| AgentState, World, Self, Goal, Thread, Focus, Memory, Affect | `KEEP/MERGE` vào state owners; xóa projection mutable trùng |
| Cognitive context/Brain contracts và llama.cpp adapter | `KEEP`, đổi từ shadow naming sang canonical sau cutover |
| DirectorLoop execution shell, hard arbiter, transactions | `MERGE` vào Turn Kernel/execution boundary |
| Legacy Director soft policy và Director V2 selector/materializer/takeover | `DELETE` sau Brain canary cho toàn mode tương ứng |
| AutonomyEngine, ProactiveHosting, SelfTalkPlanner, BehaviorLibrary decision logic | `MERGE` state/opportunity hữu ích; sau đó `DELETE` soft owner trùng |
| AgentContextRenderer, ConversationContextComposer, ContextSelector | `MERGE` vào một `CognitiveContextBuilder`, rồi xóa renderer trùng |
| RuleFilter correctness guard | `MERGE` hard-invalid checks vào validator |
| Style/dedup regenerator và word/sentence/question clamp | `DELETE`; kernel chỉ reject/suppress, không rewrite |
| TTS/subtitle/audio/VTS/OBS adapters và verifier | `KEEP/MERGE` dưới execution |
| DecisionRecord, trajectory, cognitive observer records | `MERGE` thành turn/outcome journal |
| A/B, replay, blind review, soak, release scripts | `OFFLINE`; không compose live |
| Mock loop, dormant observer, migration dashboard/toggle/config | `DELETE` khi consumer/gate tương ứng đóng |
| Frozen `ver/v1.0` và sanitized historical evidence | `ARCHIVE`; không sửa hoặc import production |

Inventory S0 mới có quyền kết luận file cụ thể thuộc dòng nào; bảng này không cấp quyền xóa theo tên đoán.

### 13.1.5. Quy trình một wave

Mỗi wave là một change độc lập:

1. đọc owner/source/import/config/test hiện tại và chụp baseline;
2. docs-first ghi exact file move/merge/delete, compatibility adapter và rollback;
3. owner duyệt;
4. move cơ học trước, giữ behavior và dùng compatibility re-export có hạn nếu cần;
5. gộp owner sau khi import graph đã ổn;
6. chạy import-boundary, targeted, impacted và full regression;
7. replay/canary nếu behavior/output/decision thay đổi;
8. báo before/after về live-reachable file, dependency, feature toggle, config owner và duplicate owner;
9. commit đúng một wave rồi dừng.

Không trộn move hàng loạt, đổi behavior, tune model và xóa fallback trong cùng commit. Nếu move cơ học làm
thay output thì wave fail và phải tách lại.

#### 13.1.5.1. S1 — exact interface-boundary cut

S1 là một lần chuyển quyền sở hữu **type/contract**, không phải chuyển quyền sở hữu behavior. Exact scope được
khóa như sau:

- tạo `interfaces/events.py` làm canonical owner cho event enums, provenance và `GroundedEvent` hiện nằm tại
  `services/agent/types.py`;
- tạo `interfaces/state.py` làm canonical owner cho immutable agent/thread/goal snapshots, proposals và các
  crossing decision DTO hiện nằm tại `services/agent/types.py`, `services/agent/goal_types.py`,
  `services/agent/goal_proposal.py`, `services/agent/thread_extraction.py` và
  `services/agent/behavior_library.py`;
- chuyển relationship DTO từ `services/relationship/types.py` vào `interfaces/relationship.py`, và chuyển
  evaluation DTO từ `services/evaluation/types.py` vào `interfaces/evaluation.py`;
- `interfaces/agent.py` chỉ được import từ `interfaces`; toàn bộ production consumer của các DTO trên phải dùng
  canonical interface import;
- giữ các module type cũ làm compatibility re-export cùng object identity tới S8; module implementation chứa
  `GoalProposal`, `ThreadExtraction`, `BehaviorKind` và `BehaviorDecision` cũng re-export canonical type để
  caller cũ không vỡ trong S1.

Compatibility adapter không được copy class, subclass, convert payload hoặc đổi validation/default/enum value.
Test phải chứng minh old/new symbols là cùng object (`is`), serialized shape và exception behavior giữ nguyên.
Adapter có removal wave `S8` và chưa được xóa khi repository còn importer bên ngoài canonical production path.

S1 bổ sung AST dependency guard với các invariant: mọi module `interfaces` không import `services` hoặc
`orchestrator`; canonical production source không mở thêm import type từ compatibility module; interface graph
không có strongly-connected component nhiều hơn một module. Inventory S0 được cập nhật theo file/test count và
boundary finding, nhưng `source_deletion_authorized` tiếp tục `false`.

Implementation S1 ngày 26/08/2026 đã áp dụng exact scope này: dependency interface→implementation giảm từ bảy
module xuống `0`; bốn compatibility type module còn tồn tại nhưng đều có `0` live-reachable importer. Production
module tăng `197 -> 199` do thêm hai canonical contract owner, trong khi static live-reachable giảm `149 -> 148`.
Full offline đạt `2.464 passed`, `0` lỗi; không có replay/blind vì không đổi behavior/output/decision. Source vẫn
dirty từ WIP trước S1 nên evidence không mở release gate và compatibility adapter chưa được xóa.

Không đổi trong S1: entrypoint/composition, feature state, YAML/prompt/metric, Director/Brain/kernel authority,
LLM call, scheduler, transaction, delivery, state mutation, public speech hoặc product version. Vì là exact move,
không cần replay/blind nếu targeted, impacted và full offline regression chứng minh equivalence. Rollback là đổi
production import về compatibility paths; không có data/config migration.

#### 13.1.5.2. S2 — canonical ingress và authoritative state cut

S2 chỉ bắt đầu code từ một checkpoint S1 riêng có thể rollback. Audit sau S1 ghi nhận live còn ba event shape
(`InputEvent`, `GroundedEvent`, `PerceptionEvent`), hai admission/dedup history (`EventLedger` và
`PerceptionIngress`) và hai reducer mutable chính (`AgentStateReducer`, `WorldModelShadow`). Ngoài GoalManager
audit callback, có tám call site trực tiếp ghi `GroundedEvent` vào AgentState, ba call site trực tiếp gọi
World `apply_event` và một relationship write từ ChatRouter. Đây là nhiều cửa ghi state, không phải một
authoritative state layer.

Contract đích của S2:

```text
YouTube/Discord/System/OBS/verified outcome
  -> source adapter
  -> CanonicalEventNormalizer
  -> CanonicalEventIngress.submit(CanonicalEvent)
  -> AuthoritativeStateReducer.apply(CanonicalEvent)
       -> Agent/Thread/Goal/Recap projection
       -> World projection khi event_type thuộc world domain
       -> relationship observer chỉ từ pseudonymous viewer_ref
  -> AuthoritativeStateSnapshot
```

`CanonicalEvent` là immutable DTO mới trong `interfaces/events.py`, có `schema_version`, `event_id`,
`event_type`, `source`, UTC `occurred_at`, bounded JSON-safe `payload`, provenance, confidence và `dedup_key`.
Normalizer là nơi duy nhất chuyển `InputEvent`, `GroundedEvent` hoặc `PerceptionEvent` cũ sang shape này.
Không producer nào được tự tạo state-specific event rồi gọi store/reducer trực tiếp sau cutover.

`AuthoritativeStateSnapshot` và `AuthoritativeStateService` thuộc `interfaces/state.py`. Snapshot aggregate chỉ
tham chiếu immutable Agent/World/Self/Goal/Relationship projections hiện có; không tạo bản mutable thứ hai.
Reducer định tuyến theo `event_type`, giữ exact event id/timestamp/confidence/evidence, và listener phụ như
relationship/goal phải failure-isolated như behavior hiện tại. Raw viewer identity chỉ tồn tại trong source
adapter đủ lâu để tạo pseudonymous `viewer_ref`; không được vào CanonicalEvent, ledger, metric hoặc log.

Package cut của S2:

- tạo `services/ingress/normalizer.py`, `services/ingress/adapters.py` và chuyển canonical admission khỏi
  `services/perception`;
- tạo `services/state/authoritative.py`; move cơ học AgentState/EventLedger, World reducer và Self projection
  vào `services/state`;
- giữ old `services.agent`, `services.perception`, `services.world` và `services.self_model` path dưới dạng
  exact compatibility re-export/adapter đến S8; Goal/Thread/Relationship/Memory implementation chưa move hàng
  loạt trong S2 vì continuity/store ownership được đóng ở S6;
- tạo `config/state.yaml` làm canonical owner cho giá trị hiện có của agent/perception/world/self/relationship;
  loader alias cũ chỉ đọc và có removal wave S8, không được tạo hai bộ threshold khác nhau;
- cập nhật StreamRuntime và mọi production writer để chỉ nhận canonical ingress/state interface. Dashboard và
  context lấy immutable projection từ authoritative snapshot, không gọi mutable implementation.

Không đổi trong S2: event meaning, ordering, TTL, capacities, feature enabled state, relationship privacy,
memory semantics, Brain/Director/kernel authority, scheduler, LLM prompt/model/sampling, speech/action delivery,
product version hoặc public output. Mock/evaluation path không được trở thành live owner. `event_bus.py` và
`state_machine.py` không được hồi sinh; chúng giữ disposition S0 và chưa được xóa.

Acceptance bắt buộc: zero production state writer ngoài canonical reducer; zero live importer của compatibility
implementation path; one dedup decision per canonical event; old/new snapshot serialization và event ordering
equivalent cho chat, donation, environment, speech, goal audit; relationship privacy/idempotency giữ nguyên;
interface/import guard, targeted/impacted/full offline xanh. Vì reducer path đổi, deterministic replay so sánh
event lineage + snapshot là bắt buộc dù public text không đổi; blind review chỉ cần khi output/decision đổi.
Rollback đổi composition về compatibility ingress/state adapter từ checkpoint S1, không migrate hoặc rewrite data.

### 13.1.6. Rollout behavior trong quá trình chuẩn hóa

| Trạng thái | Public soft owner | Brain | Compatibility | Failure trước reservation |
|---|---|---|---|---|
| `OFF` | Compatibility | Không chạy | Public | Compatibility xử lý |
| `SHADOW` | Compatibility | Read-only | Public | Observer record |
| `CANARY` | Owner đã chọn trước lượt | Public trong exact scope | Chỉ rollback cho lượt sau | `WAIT` nếu Brain là owner |
| `PRIMARY` | Brain | Public | Không gọi trên cùng lượt | `WAIT` |
| `RELEASED` | Brain | Public | Đã rời live graph | `WAIT` |

Một lượt không được có hai soft owner. Rollback trước `RELEASED` chuyển toàn scope về compatibility từ lượt
kế tiếp. Sau `RELEASED`, rollback bằng release artifact/tag, không giữ legacy implementation được compose
vĩnh viễn.

### 13.1.7. Ánh xạ sang gate MCB hiện hữu

MCB không quyết định cấu trúc; nó chỉ xác minh các wave khi behavior được chuyển:

| Gate | Wave được kiểm chứng |
|---|---|
| MCB-4 | S0–S3: inventory, boundary, exact-live Context/Brain dry-run |
| MCB-5 | S4–S5 cho `READ_CHAT` |
| MCB-6 | S6 cho scheduler, `SELF_TALK`, `FOLLOW_UP`, Thread/Focus |
| MCB-7 | S6 cho Memory proposal/commit |
| MCB-8 | S5 cho embodiment/external action |
| MCB-9 | Tối ưu model/context trong S3 đã khóa, không đổi cấu trúc |
| MCB-10 | S7–S8, full cutover, delete và release |

### 13.1.8. Gate chung và stop condition

- `0` false commit, duplicate delivery/action, permission bypass, hard-safety violation hoặc fabricated
  success;
- interface/import graph không cycle và không vi phạm chiều dependency;
- live runtime không import evaluation/corpus/release tooling;
- invalid/stale/timeout trước reservation tạo `WAIT`, không tạo fallback text hoặc mutate state;
- targeted, impacted V1 regression và full offline suite xanh;
- cùng adapter cho live/replay; output change phải qua blind review và canary;
- mỗi wave không được tăng duplicate owner; nếu chưa giảm được phải chứng minh đó là adapter tạm có removal
  gate;
- quality không thắng chỉ được tune model/prompt/sampling/context, cấm thêm rewriter/Director/judge;
- file chỉ được xóa khi replacement owner, live importer, config/metric/dashboard/test consumer và rollback đã
  được xác minh.

### 13.1.9. Chuẩn hóa blind review có thời gian

Evaluation đích phải tách hai phép đo độc lập:

1. **Content blind:** chấm language, context, character, spontaneity, grounding và AI-smell từ cùng canonical
   pre-turn state. Artifact văn bản tĩnh không được chấm live timing; field cũ `timing` phải là `N/A` hoặc đổi
   thành `situational_appropriateness` để không tuyên bố quá bằng chứng.
2. **Temporal blind:** chấm response delay, interruption, turn spacing, follow-up, self-talk timing và cảm giác
   nhịp hội thoại từ temporal replay/audio A/B. Hai candidate dùng cùng event timeline, TTS voice, playback
   rate, hardware profile và initial state; role A/B được seal/swap trước review.

Turn/outcome journal phải ghi timestamp monotonic/UTC và lineage tối thiểu:

```text
event_received_at -> opportunity_opened_at -> brain_started_at -> first_token_at
-> turn_completed_at -> delivery_reserved_at -> audio_started_at -> audio_finished_at
-> interrupted_at? -> outcome_committed_at
```

Temporal artifact chỉ công bố relative timeline, current/prior event, khoảng im lặng và audio/text đã
sanitized; không lộ build role, prompt, raw identity, secret hoặc internal failure detail. `WAIT` phải được
review trong cửa sổ sự kiện tiếp theo, không hiển thị như một dòng tĩnh: chờ để nhận chat tiếp là behavior khác
với bỏ quên câu hỏi trực tiếp.

Automated metrics và human score không thay thế nhau. Máy tính event-to-opportunity, opportunity-to-first-token,
event-to-first-audio, audio-finish-to-next-speech, event age, interrupt reaction và silence/follow-up delay;
người chấm đánh giá nhịp đó có tự nhiên trong ngữ cảnh hay không. S4–S6 hoặc bất kỳ wave nào đổi scheduler,
latency, delivery hay continuity đều phải chạy temporal replay/blind tương ứng; content blind cũ không được
dùng làm timing gate.

### 13.1.10. Definition of Done

Chuẩn hóa hoàn tất khi repository khớp cấu trúc đích; live graph có một ingress boundary, một authoritative
state layer, một ContextBuilder, một Brain, một Turn Kernel, một execution/verification boundary, một Outcome
Committer và một operations surface; `StreamRuntime` chỉ composition/lifecycle; evaluation hoàn toàn offline;
không còn post-generation rewrite hoặc soft owner cũ; config/test tree khớp owner; migration toggle/wrapper đã
xóa; full regression, clean replay, content + temporal blind review, live canary, soak và rollback artifact
đều đạt. Giảm LOC
không phải mục tiêu độc lập, nhưng duplicate owner/live dependency/toggle/config phải giảm đo được sau mỗi
wave.

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
