# 08 — Security, privacy và recovery

> **Applies to:** Mai `1.4.0` (baseline `1.0.0`)
>
> Recovery phải giữ provenance/version; không restore dữ liệu cũ như thể nó thuộc schema mới.

## 1. Threat boundaries

Nguồn không tin cậy gồm chat text, platform metadata, LLM output, dashboard request và file backup từ
bên ngoài. Credential, raw identity và private transcript là dữ liệu nhạy cảm. Localhost không tự
đồng nghĩa an toàn nếu máy có process/user khác.

## 2. Credential

- Discord token chỉ đọc từ env var `DISCORD_BOT_TOKEN` hoặc tên cấu hình tương đương.
- Không ghi token vào YAML, log, snapshot, evidence hoặc command history chia sẻ.
- YouTube video ID không phải secret nhưng vẫn validate format/trạng thái platform.
- Không commit `.env`; `.env.example` chỉ chứa tên biến/placeholder.
- Preflight chỉ báo token present/missing, không render giá trị.

Nếu token lộ: revoke/rotate tại platform trước, xóa khỏi shell history/log/backup rồi mới restart.

## 3. Prompt injection và unsafe chat

Chat là data, không phải system instruction. Classifier/filter xử lý jailbreak category nhưng hard
safety không được phụ thuộc việc model “nghe lời”. Không đưa secret, system prompt raw hoặc operator
credential vào user-visible output. Decision/evidence chỉ giữ reference bounded.

## 4. PII và identity

Persistent viewer identity dùng local salt ở `data/privacy_salt.bin`. `mask_pii()` che email, phone,
sensitive URL và known identifier trước JSONL/evidence. Salt không được commit/chia sẻ; mất salt có thể
làm identity mapping không còn nối được, vì vậy backup salt phải theo policy riêng và mã hóa phù hợp.

`config/data_privacy.yaml` hiện dùng operator notice, retention review 30 ngày và không auto-delete.
Đây là policy review thủ công, không phải cam kết hệ thống tự xóa sau 30 ngày.

## 5. Data minimization

- Agent state chỉ giữ recent bounded events.
- Affect cause chỉ giữ source event ref.
- Decision record dùng candidate summary/evidence ref, không raw chat dump.
- Relationship memory có TTL/max item và pseudonymous profile.
- Dashboard snapshot bounded.
- Evaluation/release evidence sanitized và không chứa raw transcript.
- Dataset chỉ nhận turn có delivery outcome xác thực; pending/failed attempt ở raw journal nhưng bị
  quarantine khỏi train artifact.
- Xóa corpus lỗi thời phải backup trước, verify SHA-256, liệt kê exact target và không đụng DB/salt.

## 6. Emergency model

Emergency stop phải thực hiện theo thứ tự:

1. đóng speech gate;
2. đóng environment action gate;
3. cancel active synthesis/audio;
4. pause Director/recovery;
5. ghi operator action/incident;
6. chỉ resume sau khi prune stale goal và root cause được đánh giá.

Nếu hotkey toàn cục không hoạt động do quyền Windows, Ctrl+C là fallback graceful; không dùng mass
`Stop-Process -Name python -Force`.

## 7. Recovery ladder

| Mức | Khi dùng | Hành động |
|---|---|---|
| 1 — Retry service | lỗi transient, dưới threshold | HealthSupervisor bounded retry/backoff |
| 2 — Degraded mode | TTS/memory/log phụ lỗi | subtitle/working memory/buffer, tiếp tục có metric |
| 3 — Pause | correctness chưa rõ | ngừng action mới, giữ dashboard/log |
| 4 — Emergency stop | unsafe output/action | đóng gate + cancel ngay |
| 5 — Graceful restart | config/model/service cần reset | Ctrl+C, review snapshot, start lại |
| 6 — Restore | data corrupt/migration lỗi | verify checksum, stop runtime, apply restore |

Không reset circuit liên tục để che crash deterministic.

## 8. Rollback matrix

| Thay đổi | Rollback |
|---|---|
| Hybrid mood prompt | `features.mood_v2_prompt.enabled=false` |
| Operator dashboard v2 | mở `/legacy` hoặc tắt feature |
| Semantic memory | chạy không `-Memory`; fallback working memory |
| TTS primary | subtitle degraded mode; `-NoTts` chỉ để cô lập lỗi |
| New optional feature | tắt toggle sau dependency check |
| Config hot reload lỗi | loader giữ config cũ; sửa YAML rồi reload |
| Model candidate | trỏ lại GGUF production đã biết |
| DB migration | stop runtime, restore pre-migration copy |
| JSONL data | verify manifest rồi restore backup |

Rollback không được xóa evidence incident hoặc làm giả transaction committed.

## 9. Backup và restore

```powershell
# Xem trước
.\venv\Scripts\python.exe scripts\backup_data.py --dry-run

# Backup thật
.\venv\Scripts\python.exe scripts\backup_data.py

# Verify backup, chưa ghi
.\venv\Scripts\python.exe scripts\restore_data.py `
  backups\data\runtime_logs\backup_<UTC>

# Sau graceful stop, restore file chưa tồn tại
.\venv\Scripts\python.exe scripts\restore_data.py `
  backups\data\runtime_logs\backup_<UTC> --destination logs --apply

# Ghi đè chỉ khi operator đã xác minh target
.\venv\Scripts\python.exe scripts\restore_data.py `
  backups\data\runtime_logs\backup_<UTC> --destination logs --apply --overwrite

# Dataset artifact dùng destination riêng
.\venv\Scripts\python.exe scripts\restore_data.py `
  backups\data\dataset_artifacts\backup_<UTC> --destination data\datasets --apply
```

Restore reject absolute/path traversal và checksum mismatch. Nó không xóa file ngoài manifest.
Backup config có nhiều source độc lập: `runtime_logs` và `dataset_artifacts`. Mỗi source tạo manifest
riêng để restore không trộn raw journal với artifact đã canonicalize. Dataset bundle là immutable; khi
cần build lại phải tạo dataset ID mới, không sửa file trong bundle cũ.

Khi restore qua một product/schema release mới, verify checksum mới chỉ chứng minh file nguyên vẹn.
Operator vẫn phải chạy compatibility gate/adapter của release đích; không sửa `schema_version` thủ công
để ép dữ liệu v1.0.0 qua gate.

## 10. Incident handling

Incident record cần severity, subsystem, sanitized summary, timestamp, status và resolution action.
Không dán raw prompt/chat/token vào summary. Với critical incident:

1. emergency/pause;
2. lưu correlation IDs và snapshot;
3. xác định committed vs released;
4. cô lập subsystem bằng feature/launcher flags;
5. reproduce bằng deterministic test/fault injection;
6. fix + regression;
7. resolve incident với action cụ thể;
8. post-stream review lại trước live.
