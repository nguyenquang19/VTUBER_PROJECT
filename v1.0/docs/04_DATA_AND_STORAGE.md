# 04 — Data contracts và storage

> **Applies to:** Mai `1.4.1` (baseline `1.0.0`)
>
> **Frozen data matrix:** architecture `mai-agent-v1`, turn `3`, delivery `1`, canonical `1`.
>
> **Từ `1.1.0`:** wire-format của journal có model tường minh (`services/data/record_schema.py`) và
> fingerprint registry (`config/data_schema_registry.yaml`); validate tại write-time, không khớp thì
> quarantine. Field của turn/delivery/canonical không đổi so với baseline.

## 1. Contract chính

Các contract runtime dùng Pydantic model hoặc frozen dataclass. Không truyền dict tự do qua boundary
nếu đã có model trong `interfaces/`.

### `InputEvent`

Input chuẩn hóa từ platform: ID, UTC timestamp, source enum, optional user ID/name, content và
metadata. Raw ID/name được phép tồn tại trong memory của turn nhưng persistent sink phải sanitize.

### `LLMRequest` và `LLMToken`

`LLMRequest` chứa request ID, prompt hoặc messages, max tokens, temperature, optional seed và stop
sequences. Nếu có `messages`, chúng thắng `prompt`. `LLMToken` phải giữ order, có final marker và
metadata bounded.

### `ParsedResponse`

Internal output sau parser gồm text sạch, mood parsed nếu có, continuation/reason và `ok`. Raw output
chỉ dùng debug/filter evidence đã sanitize; text gửi TTS không chứa reasoning/meta block.

### `TurnAffect`, `SessionMood`, `ResponsePlan`

- TurnAffect: style, response mode, energy, warmth, urgency, cause ref, TTL.
- SessionMood: valence, arousal, irritation và thời điểm update.
- ResponsePlan: một directive thống nhất cho generation/delivery style.

Các trục đều bounded; cause ref không chứa raw message.

### `ActionTransaction`

Gồm transaction ID, idempotency key, action, state, timestamps và bounded reason. State chỉ đi theo
state machine trong tài liệu pipeline.

### `TTSDeliveryResult`

Gồm request ID, delivered, mode, tổng số câu, số câu delivered/audio/subtitle/failed và cancelled.
`delivered=true` chỉ khi mọi câu có sink và không cancelled.

### `DecisionRecord`

Gồm decision ID, action/reason/segment, evidence refs, candidate summaries, rejection reason,
transaction/delivery outcome. Record phục vụ operator; không phải raw reasoning trace của model.

## 2. Persistent storage map

| Path | Format | Writer | Nội dung | Restore/retention |
|---|---|---|---|---|
| `logs/events.jsonl` | JSONL | structlog `JsonlWriter` | event, error, transition | rotate; backup thủ công |
| `logs/turns.jsonl` | JSONL | `TurnLogger` | generation attempts schema v3 (validate write-time) | append-only segment; rotates |
| `logs/delivery_outcomes.jsonl` | JSONL | `TurnLogger` | delivered true/false keyed by turn (validate write-time) | append-only segment; rotates |
| `logs/quarantine.jsonl` | JSONL | `TurnLogger` | record không khớp schema + lý do; KHÔNG dùng train | append-only; debug drift |
| `logs/pref_pairs.jsonl` | JSONL | LLMTurnRunner | chosen/rejected DPO pairs | data quality gate |
| `logs/ratings.jsonl` | JSONL | dashboard | human rating | export dataset |
| `logs/corrections.jsonl` | JSONL | dashboard | operator correction | export dataset |
| `logs/live/subtitle.txt` | UTF-8 text | SubtitleFallback | câu subtitle mới nhất | atomic replace, OBS reads |
| `logs/operations/incidents.jsonl` | JSONL | IncidentLog | sanitized incident ledger | resolve, post-review |
| `logs/operations/operator_audit.jsonl` | JSONL | control plane | operator mutations | append-only |
| `logs/operations/last_runtime_snapshot.json` | JSON | shutdown | final bounded runtime state | standalone dashboard/review |
| `data/mai.db` | SQLite | migrations/memory/relationship | schema, vectors, profiles | pre-migration backup |
| `data/privacy_salt.bin` | bytes | sanitizer setup | local HMAC/hash salt | không chia sẻ/commit |
| `data/datasets/<dataset_id>/` | directory | exporter | canonical/SFT/DPO/quality/manifest | immutable artifact |
| `backups/data/runtime_logs/backup_*` | files + manifest | backup script | raw JSONL + SHA-256 | restore vào `logs` |
| `backups/data/dataset_artifacts/backup_*` | files + manifest | backup script | dataset bundles + SHA-256 | restore vào `data/datasets` |
| `backups/mai.db.pre_migration_*` | SQLite copy | MigrationRunner | DB trước migration | manual DB recovery |
| `docs/baselines/*.json` | JSON | eval/release scripts | sanitized machine evidence | versioned evidence |

## 3. Turn log schema thực dụng

`LLMTurnRunner._log_turn()` ghi generation attempt schema v3. Field quan trọng khi debug:

- identity: schema version, session ID, request/turn ID;
- record type: `generation_attempt`; delivery không nằm trong record này;
- trigger: trigger type, event category;
- input/output: masked user text và Mai text;
- generation: fallback level, parse status, timing, history length;
- filter: initial/final verdict và regeneration flag;
- mood: engine snapshot/cause metadata bounded;
- data quality: raw mood-block presence, continuation;
- provenance: source/evidence refs không chứa credential.

Outcome được append riêng với `record_type=delivery_outcome`, schema v1, cùng composite identity và
`delivered` boolean. Không dựa vào attempt đơn lẻ để train. Khi đổi schema, tăng version, thêm canonical
adapter, update fixture/compatibility test; tuyệt đối không relabel raw record thành version mới.

### 3.1. Write-time validation và quarantine (từ `1.1.0`)

Wire-format của mỗi record được định nghĩa bằng model tường minh trong `services/data/record_schema.py`
(`TurnRecordV3`, `DeliveryOutcomeV1`), `extra="forbid"` — engine phải map VÀO model, không dump dict tự
do. `TurnLogger.log_turn`/`log_delivery` validate record theo model TRƯỚC khi ghi:

- khớp → ghi vào journal train như thường;
- không khớp (thiếu field bắt buộc, sai kiểu, hoặc có field lạ do engine drift) → ghi vào
  `logs/quarantine.jsonl` kèm lý do + metric `turn_quarantined_total`, KHÔNG lọt vào `turns.jsonl`.

Nhờ vậy trust boundary nằm ở write-time: mọi record trong journal train đều chứng minh được khớp schema
đã khai báo. Sửa engine làm đổi shape record sẽ bị bắt ngay (quarantine tăng), không âm thầm làm bẩn
corpus.

Fingerprint: `schema_fingerprint()` hash field-set + kiểu của model; giá trị chốt nằm ở
`config/data_schema_registry.yaml`. Startup fail-fast nếu model lệch fingerprint đã chốt (đổi model mà
quên bump version). Fingerprint được đóng vào manifest dataset để ràng dataset với đúng schema sinh ra nó.
Khi cần đổi schema: thêm `TurnRecord<V+1>` + fingerprint mới + adapter canonical mới; version cũ vẫn
canonicalize qua adapter cũ.

## 4. Ba lớp lưu trữ bền vững

| Lớp | Có được sửa? | Mục đích |
|---|---|---|
| Raw journal segment | append-only tới khi rotate | giữ bằng chứng đúng thời điểm phát sinh |
| Canonical | tạo lại từ raw | ổn định field cho consumer, có source schema + adapter ID |
| Dataset bundle | immutable | snapshot phục vụ train/eval, có manifest và checksum |

Compatibility được khai báo trong `eval/contracts/mai_agent_v1.yaml` theo từng trục schema, persona,
architecture, context và agenda. Version khác không mặc định là rác: nếu contract cho phép và code có
adapter thì được tái sử dụng; nếu thiếu provenance/delivery hoặc không có adapter thì quarantine. Mỗi
dataset dùng split theo session cố định, do đó cùng một session không rò giữa train/validation/holdout.

### 4.1. Dataset bundle v1.0.0

```text
data/datasets/<dataset_id>/
  manifest.json
  quality_report.json
  canonical/turns.jsonl
  sft/train.jsonl
  sft/validation.jsonl
  sft/holdout.jsonl
  dpo/train.jsonl
  dpo/validation.jsonl
  dpo/holdout.jsonl
```

Manifest giữ dataset ID, timestamp UTC, contract/schema/adapter, compatible persona, checksum/size nguồn
và count. Dataset directory đã publish là immutable; build lại tạo ID mới. Raw segment có thể bị xóa bởi
rotation sau giới hạn `max_size_mb × (keep_files + active file)`, nên backup là lớp lưu dài hạn thật.

### 4.2. SFT/DPO format v2 — multi-turn, no-directive (từ `1.2.0`)

SFT (`sft_schema` 2) không còn là cặp lẻ 1 lượt. Mỗi example là **cả mạch hội thoại**: `build_sft`
group theo `session_id`, sort `turn_id`, dựng history từ các lượt **đã delivered** trong sliding window
(`--history-window`, mặc định 8), rồi:

```
messages = [persona] + (user/assistant các lượt delivered trước) + user hiện tại + assistant target
```

**KHÔNG chèn `context_block`** (mood/cause/stage directive). Mục tiêu: model học mood/nhịp từ chính mạch
hội thoại, không phụ thuộc directive scaffolding → giọng tự nhiên hơn, không dính format prompt hiện tại.

DPO (`dpo_schema` 2): prompt mang **cùng context multi-turn** (bỏ directive); cặp `chosen/rejected` luôn
từ **cùng một lượt** (regen hoặc correction) — preference chỉ có nghĩa trong đúng hội thoại đó.

LLM-judge (`services/evaluation/quality_judge.py`, bật bằng `--judge-min-score`): chấm mỗi SFT candidate
CÙNG context theo rubric (đúng chất persona, mạch lạc, không nhạt, không bịa); dưới ngưỡng thì loại. Đây
là tầng chống rác **ngữ nghĩa** (gate cấu trúc không bắt được). Judge lỗi → giữ example (fail-safe).

Inference: cờ `models.yaml::llm_main.inject_mood_directive` (mặc định `true`). Đặt `false` để bỏ luôn mood
directive ở live — **chỉ bật sau khi model fine-tuned đã đọc được mood từ context** (đo trên holdout), nếu
không base model hiện tại sẽ nói nhạt. Train và inference phải cùng phía (cùng bỏ directive).

## 5. SQLite

Migration files:

- `001_initial.sql`: schema nền;
- `004_add_memory_tables.sql`: memory/vector tables;
- `005_add_relationship_tables.sql`: profile/note/narrative/gag;
- `006_add_relationship_positive_events.sql`: positive interaction tracking.

`MigrationRunner` backup DB trước mỗi migration rồi chạy transaction. Migration fail không được đánh
dấu applied. Không sửa migration đã phát hành; thêm file số thứ tự mới.

## 6. In-memory bounded state

Các cấu trúc không bền qua restart nhưng phải bounded:

- EventBus subscriber queues;
- SaliencePool/chat candidates;
- recent decision/transaction cache;
- pending delivery map;
- degraded logging buffer;
- working memory;
- recent agent events/open threads/goals;
- audio queue và cancel set;
- dedup/recent opener buffers.

Mọi cấu trúc mới trong live loop cần max size/TTL từ YAML và metric high-water/drop/evict phù hợp.

## 7. Commit semantics theo loại dữ liệu

| Dữ liệu | Trước delivery | Sau delivery success | Sau delivery failure |
|---|---|---|---|
| LLM parsed text | pending | có thể log/commit history | không commit history nghiệp vụ |
| Chat candidate | vẫn trong pool | remove/complete | giữ hoặc retry theo policy |
| Goal/segment | chưa advance | commit transition/progress | giữ nguyên |
| Memory | chưa schedule với deferred turn | extract/write async | skip |
| Speech event | chưa phát | append grounded event | skip |
| Decision record | reserved/delivering | committed | released/not_delivered |
| Diagnostic log | có thể ghi | ghi outcome | ghi failure outcome |

Diagnostic logs được phép ghi trước commit vì chúng mô tả attempt; không được dùng chúng như bằng
chứng action nghiệp vụ đã hoàn tất.

## 8. Backup và restore contract

`backup_data.py` chỉ copy source, không xóa. Nó backup riêng raw runtime journal và dataset artifact.
Manifest schema 1 chứa path tương đối, size và SHA-256.
`restore_data.py` mặc định verify-only, reject path traversal/checksum mismatch và refuse overwrite nếu
không có `--overwrite`. Restore không xóa file ngoài manifest.

## 9. Conversation thread contract

`OpenThread` is immutable and bounded. It contains topic/summary, status, evidence, Mai claims, viewer
contributions, open questions, last/next public move, move count, timestamps and origin event ID.
Every contribution retains a `source_event_id`; raw model reasoning is never stored.

Threads are in-memory session state. Active threads become parked after `park_after_seconds`, may resume
when related grounded chat arrives, and expire at TTL. Claims and move progress caused by Mai output are
post-delivery data: delivery failure leaves them unchanged.
