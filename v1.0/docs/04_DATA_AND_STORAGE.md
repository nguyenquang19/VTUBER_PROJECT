# 04 — Data contracts và storage

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
| `logs/turns.jsonl` | JSONL | `TurnLogger` | one sanitized record/turn | input eval/export |
| `logs/pref_pairs.jsonl` | JSONL | LLMTurnRunner | chosen/rejected DPO pairs | data quality gate |
| `logs/ratings.jsonl` | JSONL | dashboard | human rating | export dataset |
| `logs/corrections.jsonl` | JSONL | dashboard | operator correction | export dataset |
| `logs/live/subtitle.txt` | UTF-8 text | SubtitleFallback | câu subtitle mới nhất | atomic replace, OBS reads |
| `logs/operations/incidents.jsonl` | JSONL | IncidentLog | sanitized incident ledger | resolve, post-review |
| `logs/operations/operator_audit.jsonl` | JSONL | control plane | operator mutations | append-only |
| `logs/operations/last_runtime_snapshot.json` | JSON | shutdown | final bounded runtime state | standalone dashboard/review |
| `data/mai.db` | SQLite | migrations/memory/relationship | schema, vectors, profiles | pre-migration backup |
| `data/privacy_salt.bin` | bytes | sanitizer setup | local HMAC/hash salt | không chia sẻ/commit |
| `backups/data/backup_*` | files + manifest | backup script | JSONL copies + SHA-256 | verify before restore |
| `backups/mai.db.pre_migration_*` | SQLite copy | MigrationRunner | DB trước migration | manual DB recovery |
| `docs/baselines/*.json` | JSON | eval/release scripts | sanitized machine evidence | versioned evidence |

## 3. Turn log schema thực dụng

`LLMTurnRunner._log_turn()` ghi schema versioned. Field quan trọng khi debug:

- identity: schema version, session ID, request/turn ID;
- trigger: trigger type, event category;
- input/output: masked user text và Mai text;
- generation: fallback level, parse status, timing, history length;
- filter: initial/final verdict và regeneration flag;
- mood: engine snapshot/cause metadata bounded;
- data quality: raw mood-block presence, continuation;
- provenance: source/evidence refs không chứa credential.

Không dựa vào field không versioned trong downstream exporter. Khi đổi schema, tăng version, update
exporter, fixture và compatibility test.

## 4. SQLite

Migration files:

- `001_initial.sql`: schema nền;
- `004_add_memory_tables.sql`: memory/vector tables;
- `005_add_relationship_tables.sql`: profile/note/narrative/gag;
- `006_add_relationship_positive_events.sql`: positive interaction tracking.

`MigrationRunner` backup DB trước mỗi migration rồi chạy transaction. Migration fail không được đánh
dấu applied. Không sửa migration đã phát hành; thêm file số thứ tự mới.

## 5. In-memory bounded state

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

## 6. Commit semantics theo loại dữ liệu

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

## 7. Backup và restore contract

`backup_data.py` chỉ copy source, không xóa. Manifest schema 1 chứa path tương đối, size và SHA-256.
`restore_data.py` mặc định verify-only, reject path traversal/checksum mismatch và refuse overwrite nếu
không có `--overwrite`. Restore không xóa file ngoài manifest.

## 8. Conversation thread contract

`OpenThread` is immutable and bounded. It contains topic/summary, status, evidence, Mai claims, viewer
contributions, open questions, last/next public move, move count, timestamps and origin event ID.
Every contribution retains a `source_event_id`; raw model reasoning is never stored.

Threads are in-memory session state. Active threads become parked after `park_after_seconds`, may resume
when related grounded chat arrives, and expire at TTL. Claims and move progress caused by Mai output are
post-delivery data: delivery failure leaves them unchanged.
