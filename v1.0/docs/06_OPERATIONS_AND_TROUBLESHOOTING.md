# 06 — Operations và troubleshooting

> **Applies to:** Mai `1.3.0` (baseline `1.0.0`)
>
> Lệnh trong tài liệu dùng PowerShell trên Windows 11.

## 1. Preflight và khởi động

Yêu cầu: Windows 11, Python 3.11+, llama.cpp binary, Gemma GGUF, TTS reference audio và credential
platform tương ứng.

```powershell
# Chỉ kiểm tra, runtime chưa chạy nên defer HTTP health:
.\venv\Scripts\python.exe scripts\live_preflight.py `
  --platform youtube --video "VIDEO_ID" --skip-server-health

# Live YouTube:
.\scripts\start_live.ps1 -Platform youtube -VideoId "VIDEO_ID"

# Live Discord:
$env:DISCORD_BOT_TOKEN = "YOUR_TOKEN"
.\scripts\start_live.ps1 -Platform discord
```

Launcher mặc định bật TTS/dashboard. `-Memory` bật semantic memory; `-NoTts` hoặc `-NoDashboard` chỉ
dùng để cô lập lỗi. Preflight report nằm ở `logs/operations/live_preflight.json` và không chứa token.

Runtime tự start llama-server khi `live_operations` và `manage_llama_process` bật. Health LLM vẫn là
blocking gate sau startup; preflight thành công không thay thế runtime health.

## 2. Endpoint và output vận hành

- Operator dashboard: `http://127.0.0.1:7860/`.
- Legacy dashboard: `http://127.0.0.1:7860/legacy`.
- Snapshot: `GET /api/snapshot`.
- WebSocket: `/ws` theo config.
- Subtitle OBS: `logs/live/subtitle.txt`.
- Event/generation/delivery log: `logs/events.jsonl`, `logs/turns.jsonl`,
  `logs/delivery_outcomes.jsonl`.
- Incident/audit: `logs/operations/`.

## 3. Checklist trước live

1. Chạy preflight bằng video ID/token thật.
2. Xác nhận không có instance Mai khác chiếm port 7860 hoặc llama port 8080.
3. Xác nhận audio device đúng và OBS đọc subtitle file UTF-8.
4. Mở dashboard; kiểm tra health/circuit/incident/action required.
5. Gửi một message test private/unlisted; đối chiếu decision và delivery mode.
6. Kiểm tra `mood_v2_prompt`, `action_transactions`, `decision_records`, `live_operations` enabled.
7. Chạy backup dry-run nếu session có dữ liệu cần giữ.

## 4. Trong live

- `Pause` dừng Director action mới nhưng giữ quan sát.
- `Emergency stop` phải đóng speech/action gate trước khi await cancel.
- `Resume` prune stale/expired goal trước khi mở gate.
- Circuit open nghĩa là bounded recovery đã hết; không reset liên tục khi root cause chưa sửa.
- Nếu TTS xuống subtitle, có thể tiếp tục degraded nếu mọi câu vẫn hiện đúng trong OBS.
- Nếu delivery `none/cancelled`, transaction phải release; kiểm tra work có còn để retry.

## 5. Shutdown và post-stream

Dùng Ctrl+C trong terminal. Đợi shutdown coordinator hoàn tất; không đóng cưỡng bức cửa sổ hoặc kill
mọi Python process.

```powershell
.\venv\Scripts\python.exe scripts\post_stream_review.py
.\venv\Scripts\python.exe scripts\backup_data.py --dry-run
.\venv\Scripts\python.exe scripts\backup_data.py
```

Review kiểm tra final snapshot, JSONL parse, unresolved incident và soak evidence. Backup không xóa
source.

Backup tạo hai nhánh manifest: `backups/data/runtime_logs/` và
`backups/data/dataset_artifacts/`. Raw log rotation chỉ giữ số segment cấu hình; session cần giữ lâu
phải backup sau stream, không chờ tới lúc export dataset.

## 6. Debug theo correlation ID

Ưu tiên ID thay vì đọc log theo timestamp thủ công:

1. Lấy `decision_id` từ dashboard decision record.
2. Lấy `transaction_id` và state cuối.
3. Lấy `request_id` dùng cho LLM/TTS.
4. Tìm ID trong `events.jsonl` và generation attempt trong `turns.jsonl`.
5. Join `delivery_outcomes.jsonl` bằng `session_id + request_id + turn_id`.
6. Đối chiếu delivery mode/count, transaction state và speech-completed event.

Diễn giải nhanh:

| Decision | Transaction | Delivery | Kết luận |
|---|---|---|---|
| có | committed | audio/subtitle/mixed | action thành công |
| có | released | none/cancelled | output chưa được commit |
| có | reserved/generated lâu | không có | kẹt generation/delivery; xem health/task |
| WAIT | không có | không có | bình thường nếu reason hợp lý |
| duplicate_committed | committed cũ | không phát lại | idempotency hoạt động |

## 7. Troubleshooting theo triệu chứng

### 7.1 llama.cpp không healthy

Kiểm tra:

```powershell
Test-Path 'E:\BAI_CUA_DUC\llama\llama-server.exe'
Test-Path '.\models\llm\gemma_4_12B_Q4.gguf'
Invoke-RestMethod http://127.0.0.1:8080/health
Get-Process -Name llama-server -ErrorAction SilentlyContinue
```

Khoanh vùng:

- binary/model thiếu: sửa `config/models.yaml`;
- port bị chiếm: xác định process owner, không tự kill process không thuộc Mai;
- timeout load: xem VRAM và llama stderr; tăng timeout chỉ khi model thực sự vẫn đang load;
- server healthy nhưng không stream: chạy `pytest -m llm` để tách HTTP/parser/cancel;
- reasoning rỗng: xác nhận extra flags có `--reasoning off`.

Nếu log có `fallback_recovered` nhưng Mai không nói, kiểm tra delivery result/transaction. Canned
fallback cố ý giữ `parse_ok=false` để bị loại khỏi dataset, nhưng text không rỗng vẫn phải đi qua
TTS/subtitle; `parse_ok` không phải delivery gate.

### 7.2 Không nhận YouTube chat

- Video ID phải là ID của live public/unlisted đang diễn ra.
- Pytchat không đọc private/ended stream tùy trạng thái platform.
- Kiểm tra poll interval và adapter health/consumer task.
- Tìm `InputEvent`/event ID trong log; nếu không có, lỗi nằm trước ChatRouter.
- Chạy `pytest tests/integration/test_youtube_simulation.py -q` để kiểm tra offline toàn tuyến
  pytchat-shaped message → YouTube adapter → ChatRouter → delivery trước khi khoanh vùng mạng/video.

### 7.3 Không nhận Discord chat

- Token phải có trong đúng terminal chạy launcher.
- Bật MESSAGE CONTENT INTENT.
- Bot có Read Message permission.
- Channel ID thuộc `chat_sources.yaml`; danh sách rỗng là accept mọi channel bot tham gia.
- Queue full dùng drop protection; xem queue/drop metric khi spam.

### 7.4 Có input nhưng Director luôn WAIT

Đối chiếu decision reason, safety hold, pause/emergency, cooldown, salience pool, active goal/thread và
chat pulse. Chạy unit test Director với snapshot tương tự trước khi đổi threshold. Đừng tăng salience
toàn cục nếu chỉ classifier/donation flag sai.

### 7.5 Director chọn sai message

Kiểm tra pooled candidate: event ID, is_super, mention, age, cluster, score. Nếu candidate đúng nhưng
decision sai, sửa arbitration. Nếu candidate không vào pool/bị evict, sửa intake/salience config.

Nếu Mai self-talk dồn dập, xem `reason`: `cold_chat/dead_air` phải cách nhau ít nhất
`director.self_talk_cooldown_seconds`; các tick giữa đó phải là `WAIT/self_talk_cooldown` hoặc
`WAIT/silence_cooldown`. `consec_read_chat_break` có thể chen sớm hơn có chủ đích. Không dùng delay TTS
để che lỗi cadence vì delay chỉ thay thời điểm bắt đầu audio, không sửa scheduling.

Nếu cadence đúng nhưng nội dung self-talk bị rời rạc, xem metric/snapshot
`self_talk_planner_stage`, `active_thought_id`, `cause`, `intention`, `commits_total`,
`releases_total`, `output_rejected_total` và `repeat_suppressed_total`. Stage đứng yên cùng release tăng
nghĩa là delivery đang fail; không được sửa bằng cách commit sớm. `pending_interrupted=true` nghĩa là
chat thật đến trong generation và output ambient đã bị chặn. Stage `wait` là chủ ý chờ chat. Nếu nội
dung bịa, kiểm tra evidence/cause và grounded context; mood directive chỉ được phép đổi style.
`WAIT/thought_unavailable` là backoff bình thường khi ledger không còn ý mới từ cùng mỏ neo; chat hoặc
evidence mới sẽ mở lại ngay, không cần hạ cooldown.

### 7.6 Output nghe “AI”, trang trọng hoặc overacting

Theo thứ tự:

1. Xác nhận event category.
2. Xem Hybrid ResponsePlan/directive trên snapshot.
3. Xem history/context có câu meta hay không.
4. Xem parsed output trước/sau filter regeneration.
5. Replay cùng input/context/seed trước khi tune sampling.

Không tăng temperature trước khi loại prompt/category sai. Wording response mode nằm ở
`affect_v2.yaml`/renderer; tone legacy nằm ở mood style config.

### 7.7 Có text nhưng không audio

Kiểm tra `tts_pipeline_last_delivery_mode`:

- `subtitle`: VieNeu lỗi nhưng fallback thành công; kiểm tra GPU/reference/TTS log;
- `none`: cả TTS và subtitle sink lỗi;
- `cancelled`: emergency/interrupt hoặc request bị cancel;
- `audio` nhưng không nghe: lỗi AudioPlayer/device/OBS mix sau synth.

Xác nhận reference audio tồn tại, VieNeu start/enroll thành công, CUDA tương thích và output device mở.
Không coi empty chunk của subtitle là audio.

Nếu log có `tts_primary_unavailable_subtitle_only`, kiểm tra `error` để phân biệt startup timeout và
health failure. Runtime chỉ được tiếp tục khi subtitle health gate xác nhận sink thật. Nếu Director
báo `director_delivery_sink_missing` hoặc `director_delivery_not_reached`, transaction phải release.

Dashboard GPU/VRAM lấy từ `nvidia-smi`. `gpu_metrics_available=false` nghĩa là query driver/command
lỗi; giá trị kèm `gpu_metrics_stale=true` chỉ là mẫu thật gần nhất, không phải số hiện tại.

### 7.8 Subtitle không hiện trong OBS

- `config/models.yaml::tts_fallback.output_file` đúng.
- Parent directory writable.
- OBS Text Source đọc file UTF-8 và refresh/read-from-file bật.
- `require_delivery=true`; nếu file write lỗi, transaction phải release.
- Kiểm tra file `.tmp`/atomic replace error trong events log.

### 7.9 Đã nói nhưng history/goal không commit

Kiểm tra TTS result có `delivered=true`, transaction có đi `DELIVERED -> COMMITTED`, pending request ID
khớp và `finalize_delivery()` được gọi đúng một lần. Nếu callback trả legacy `None`, xem compatibility
path; production TTS phải trả model typed.

### 7.10 History/goal đổi dù delivery fail

Đây là correctness bug nghiêm trọng. Reproduce bằng integration test fault injection. Kiểm tra:

- runner có `defer_delivery_commit=true`;
- side effect không xảy ra trước `_maybe_speak()`;
- failure/cancel gọi `release()` và `finalize_delivery(false)`;
- duplicate idempotency không tái commit.

### 7.11 Memory không retrieve hoặc chậm

- Không có `-Memory`: semantic memory không được compose, đây là expected.
- First BGE-M3 query có warm-up cost.
- Semantic failure phải fallback working memory.
- Xem timeout/query latency và SQLite health; không tăng prompt context vô hạn.

### 7.12 Dashboard không cập nhật hoặc báo sai

- Snapshot API có trả JSON không.
- WebSocket kết nối và push interval.
- Source snapshot (`decisions`, `operations`, `health`) có đúng trước khi nghi UI.
- `/operator` dùng v2 trực tiếp; `/legacy` tách UI rollback.
- Standalone dashboard chỉ đọc final snapshot, mutation bị khóa là đúng.
- Mood cột phải đọc `mood.mood_pos` float; `current_mood` integer có thể đứng yên vài tick do rounding.
- Badge `Snapshot offline` có thể vẫn đi cùng WebSocket healthy; nó không đồng nghĩa runtime live.

### 7.13 Log disk lỗi

JsonlWriter chuyển vào degraded bounded buffer; xem sink error/drop metric. Sửa quyền/disk rồi xác
nhận buffer flush. Logging failure không được làm Brain chết, nhưng record vượt buffer có thể drop và
phải được đếm.

## 8. Lệnh kiểm tra

```powershell
# Targeted live-delivery
.\venv\Scripts\python.exe -m pytest `
  tests\integration\test_tts_pipeline.py `
  tests\integration\test_action_transaction.py `
  tests\unit\test_subtitle_fallback.py -q

# Offline regression
.\venv\Scripts\python.exe -m pytest tests -m "not llm and not slow" --tb=short -q

# llama.cpp thật
.\venv\Scripts\python.exe -m pytest -m llm --tb=short -q

# bounded memory slow tests
.\venv\Scripts\python.exe -m pytest -m slow --tb=short -q
```
