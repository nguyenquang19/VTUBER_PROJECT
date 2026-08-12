# 07 — Testing và extension guide

> **Applies to:** Mai `1.0.2` (baseline `1.0.0`)
>
> Mọi change được phát hành sau baseline phải tăng product version và có changelog/regression evidence.

## 1. Test layers

| Layer | Mục tiêu | External dependency |
|---|---|---|
| Unit | pure policy/model/state transition/config bounds | không |
| Integration | nối nhiều service bằng fake adapter/LLM/TTS | thường không |
| Acceptance | deterministic failure matrix + invariants | không |
| Live LLM | start llama.cpp, stream/cancel thật | binary + GGUF + GPU |
| Slow | leak/bounds/soak | thời gian/tài nguyên |
| Human | naturalness/persona/operator usability | operator |

Test correctness không được phụ thuộc random clock/network. Inject seed/clock/fake service. Live test
phải có marker để offline CI deselect rõ ràng.

`tests/conftest.py` chuyển log test sang `test-results/pytest-logs` và tắt JSONL event sink. Các lỗi giả
có chủ đích trong test không được ghi vào `logs/events.jsonl` của runtime. Bộ giả lập YouTube offline
nằm tại `tests/integration/test_youtube_simulation.py`.

Replay thật do `yt-dlp` xuất dưới dạng `*.live_chat.json` có thể chạy qua đúng
`ChatRouter -> SaliencePool -> DirectorLoop` mà không cần mạng, LLM hay TTS:

```powershell
.\venv\Scripts\python.exe scripts\simulate_youtube_replay.py `
  "C:\Users\<user>\Downloads\VIDEO_ID.live_chat.json"
```

Mỗi cửa sổ thời gian trong `evaluation.youtube_replay.burst_window_ms` là một tick; mọi chat trong
cùng cửa sổ được intake đồng thời. Simulator vẫn chạy các tick trống nên dead-air/proactive pacing,
salience decay, transaction và delivery gate phản ánh một timeline live đầy đủ. Output mặc định nằm ở
`logs/evaluation/youtube_replay_simulation.json`; delivery là subtitle stub typed, không phát audio thật.
Report có `director.self_talk_cadence`; `gaps_below_configured_cooldown` phải bằng `0`, trừ test cố ý
mô phỏng `consec_read_chat_break`.
Mọi trace item có `incoming_count > 0` cũng phải không chứa delivery `self_*`; đây là regression cho
global chat-priority quiet gate. LLM stub chỉ kiểm scheduling/gate, không dùng để chấm độ tự nhiên.
Khi đổi prompt/validation, chạy thêm mẫu phân tầng bằng llama-server thật và review grounding, lặp câu,
số câu hỏi cùng latency.

### 1.1. Quy ước đặt tên và tổ chức test

- Tên file test mô tả **component/hành vi**, không mang nhãn phase/milestone (`_m3`…`_m10`, `phase1`,
  `phase2`, `m8_...`). Nhãn phase/milestone chỉ là lịch sử component, không phải trục version của test và
  gây hiểu lầm rằng test thuộc một "phase" thay vì một component.
- Vị trí quyết định loại: `tests/unit/` cho policy/model/state/config thuần; `tests/integration/` cho
  nhiều service ghép qua fake adapter/LLM/TTS.
- Một test map tới một owner component; tìm test theo component như `docs/03`, không theo mốc thời gian.
- **Một runner duy nhất:** dùng `pytest` + marker (`slow`, `llm`) và các lệnh theo vùng ở §10. Không tạo
  script gom test "theo phase" song song với pytest.
- Test dùng chung helper thì import từ file test nền có tên ổn định (ví dụ
  `tests/integration/test_director_loop.py`, `tests/unit/test_llm_turn.py`), không import chéo từ file
  mang hậu tố tạm thời.

## 2. Invariant bắt buộc

- Không commit history/memory/goal/chat removal trước delivery success.
- Một idempotency key không deliver/commit hai lần.
- Safety hold thắng donation/mood/goal.
- Mood không thay hard goal priority.
- Event/cause/evidence bounded và không lộ PII.
- Queue/cache/state có max size hoặc TTL.
- Logging/memory/dashboard failure không làm chết Brain.
- Cancel đóng output boundary và release transaction.
- Subtitle chỉ là success khi có real sink.
- Shutdown tiếp tục các bước còn lại nếu một step lỗi.

## 3. Thêm input adapter

1. Implement `InputService` trong `services/input/`.
2. Chuẩn hóa raw message thành `InputEvent`; giữ platform SDK trong adapter.
3. Thêm YAML riêng hoặc block trong `chat_sources.yaml`.
4. Register feature nếu optional, metric queue/drop/reconnect.
5. Wire adapter chỉ tại entrypoint/composition root.
6. Unit test parse/filter/channel/dedup/queue overflow.
7. Integration test ChatRouter intake và graceful stop.

Không gọi Director/LLM trực tiếp từ SDK callback.

## 4. Thêm output/delivery adapter

Nếu output là speech-like, tạo interface/implementation trả delivery result typed. Xác định rõ thế nào
là “đã giao”: enqueue nội bộ chưa chắc là external delivery. Nối adapter vào transaction boundary,
thêm cancel, timeout, fallback và no-overlap/backpressure test.

Không commit side effect chỉ vì generation thành công.

## 5. Thêm Director action

1. Thêm enum/action model.
2. Định nghĩa hard/soft arbitration và evidence bắt buộc.
3. Thêm idempotency key semantics.
4. Thêm execution path trong DirectorLoop.
5. Liệt kê side effect nào chỉ được commit sau delivery.
6. Thêm decision record mapping.
7. Test success, generation fail, filter fail, delivery fail, duplicate và cancel/shutdown window.

Action không cần output vẫn phải định nghĩa commit boundary riêng; không giả dùng TTS success.

## 6. Thêm service/feature

Checklist bắt buộc:

- interface trong `interfaces/` nếu crossing subsystem;
- implementation trong đúng `services/<domain>/`;
- full type hints, async cho I/O;
- YAML config, không magic number;
- entry trong `features.yaml` nếu optional;
- FeatureManager handler nếu runtime-toggleable;
- ít nhất một metric;
- health status có ý nghĩa;
- start/stop idempotent;
- unit + integration/failure test;
- cập nhật component/pipeline/config docs.

## 7. Thêm config field

Field phải có owner rõ, unit/range/default. Nếu ảnh hưởng runtime-critical boundary, thêm Pydantic
validation và fail-fast test. Config reload handler phải atomic; không mutate half-applied service.

Không dùng default code để âm thầm che YAML viết sai.

## 8. Thay mood/response logic

Phân biệt ba lớp:

1. Classifier: event thuộc category nào.
2. State: event tác động TurnAffect/SessionMood/legacy mood ra sao.
3. Renderer/composer: state tạo response directive nào.

Sửa đúng lớp. Mỗi thay đổi cần correctness test (safety/donation/goal unchanged), deterministic replay
cùng input/context/seed và human sample nếu đổi wording/style. Luôn giữ rollback toggle cho consumer.

## 9. Thay schema/log/storage

- tăng schema version;
- raw journal vẫn append-only, không rewrite/relabel version cũ;
- thêm canonical adapter bằng code trước khi cho version vào compatibility contract;
- test attempt pending/delivery fail không vào dataset và delivered=true mới được chọn;
- dataset bundle mới không overwrite bundle cũ, manifest phải có checksum nguồn;
- giữ reader compatibility hoặc migration rõ;
- update exporter/eval fixture;
- test corrupt/partial file;
- sanitize trước write;
- atomic replace cho snapshot/single-file output;
- append-only cho audit/incident;
- backup trước DB migration.

## 10. Test command theo vùng thay đổi

```powershell
# Director/transaction/decision
.\venv\Scripts\python.exe -m pytest tests -k "director or transaction or decision" -q

# Thought Engine: cause/evidence, quiet gate, silence one-shot, semantic question,
# stage-repeat gate và delivery transaction
.\venv\Scripts\python.exe -m pytest tests\unit\test_self_talk_planner.py `
  tests\integration\test_self_talk_human_behavior.py tests\integration\test_director_loop.py -q

# Emotion/Hybrid mood
.\venv\Scripts\python.exe -m pytest tests -k "emotion or mood or affect or hybrid" -q

# LLM/filter/prompt
.\venv\Scripts\python.exe -m pytest tests -k "llm or prompt or parser or filter" -m "not llm" -q

# TTS/delivery
.\venv\Scripts\python.exe -m pytest tests -k "tts or subtitle or delivery or audio" -q

# Operations/dashboard
.\venv\Scripts\python.exe -m pytest tests -k "dashboard or health or shutdown or emergency or incident" -q

# Data/memory/relationship
.\venv\Scripts\python.exe -m pytest tests -k "memory or relationship or export or data_quality or privacy or backup or restore or delivery_outcome" -q
```

Sau targeted test, chạy offline regression. Nếu đổi llama command/client/parser, chạy marker `llm`. Nếu
đổi queue/bounds/lifecycle, chạy marker `slow` hoặc soak tương ứng.

## 11. Review checklist

- Contract input/output có typed và documented không?
- Failure có outcome rõ hay bị swallow?
- Side effect nằm trước hay sau delivery gate?
- Có path cancel/shutdown không?
- Queue/cache có bounded không?
- Log/metric có correlation ID không?
- Có lộ token/raw viewer data không?
- Toggle OFF có rollback thật không?
- Dashboard đọc source snapshot hay tự suy luận lại?
- Test có bao phủ duplicate và partial failure không?
- Change này là patch/minor/major và đã tăng `system.app.version` chưa?
- `CHANGELOG.md`, nhãn `Applies to`, contract compatibility/migration đã cập nhật chưa?

## 12. Release/version workflow sau v1.0.0

1. Phân loại patch/minor/major theo `docs/00_V1_0_BASELINE.md`.
2. Tăng `config/system.yaml::app.version` trong cùng change.
3. Thêm changelog, nêu behavior/data/CLI và rollback/migration.
4. Cập nhật mọi tài liệu bị ảnh hưởng; không rewrite lịch sử v1.0.0.
5. Nếu contract/schema đổi, giữ reader compatibility hoặc thêm adapter/migration trước cutover.
6. Chạy documentation guard, targeted tests, replay nếu output đổi và full offline regression.

Docs-only correction sau baseline vẫn là patch release khi được chấp nhận để phát hành. Internal schema
counter hoặc tên feature v2 không tự động quyết định product major version.

## 13. Thread Engine regression

```powershell
.\venv\Scripts\python.exe -m pytest tests\unit\test_topic_matcher.py `
  tests\unit\test_conversation_move_planner.py `
  tests\unit\test_conversation_thread_lifecycle.py `
  tests\integration\test_conversation_thread_engine.py `
  tests\integration\test_youtube_replay_simulation.py -q
```

Required invariants: unrelated topics do not match, related topics resume the same thread, every retained
point has provenance, state remains bounded, and missing/failed delivery never advances a thread.

## 14. Full YouTube replay với llama.cpp thật

```powershell
.\venv\Scripts\python.exe scripts\stress_youtube_llm.py `
  "C:\path\to\video.live_chat.json" `
  --output logs\evaluation\youtube_llm_stress.json
```

The llama-server health gate must pass before generation. The command checkpoints every configured
number of calls and exits non-zero when a technical gate fails. A green technical result does not replace
operator review of the included content sample, especially grounding, relevance and naturalness.

Recompute updated gates from an existing long report without spending another llama.cpp run:

```powershell
.\venv\Scripts\python.exe scripts\stress_youtube_llm.py `
  --reanalyze-report logs\evaluation\youtube_llm_stress.json `
  --output logs\evaluation\youtube_llm_stress_assessment.json
```

The assessment fails on delivered violations. Rejected generation candidates stay visible under
`candidate_flags` and must not be mistaken for speech the audience heard.
