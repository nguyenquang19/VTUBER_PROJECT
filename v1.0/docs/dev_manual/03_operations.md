# 03 — Operations Guide

Cấu hình, chạy, monitoring, troubleshoot. Dùng cho vận hành hàng ngày (không phải xây tính năng mới).

---

## 1. Setup lần đầu

### 1.1. Prerequisites

- Windows 11
- Python 3.11+
- CUDA 12.8+ driver (RTX 5060 Ti / Blackwell)
- llama.cpp build với `GGML_CUDA=ON`, binary ở `E:\BAI_CUA_DUC\llama\llama-server.exe`
- Model `gemma_4_12B_Q4.gguf` ở `v1.0/models/llm/`

### 1.2. Windows-specific tweaks

Chạy 1 lần với PowerShell admin:
```powershell
# Long path support (nếu chưa)
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1

# Execution policy để chạy script .ps1
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# Windows Defender exception (llama.cpp Zone.Identifier có thể block)
Add-MpPreference -ExclusionPath "E:\BAI_CUA_DUC\llama\build"
```

Python với admin (cần cho `keyboard` lib global hook):
- Right-click terminal → Run as administrator
- Hoặc bỏ emergency stop hotkey (accept degradation)

### 1.3. Python venv + deps

Xác minh launcher trước. Project yêu cầu Python 3.11 trở lên; stack GPU hiện được
xác minh trên Python 3.11.9:

```powershell
cd E:\BAI_CUA_DUC\AI_VTUBER\v1.0
python --version
python -m venv venv
venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.lock.txt
```

`requirements.lock.txt` là đường cài mặc định và đã chứa đúng index cho Torch cu128
và VieNeu trên Windows. `requirements.txt` chỉ chứa dependency trực tiếp để chủ động
nâng version; không dùng file này cho fresh clone production nếu chưa resolve/test lại lock.

Nếu `venv\Scripts\python.exe` báo đường Python cũ không tồn tại, cài lại Python 3.11+
rồi tạo venv sạch. Không sao chép `venv` từ máy khác.

Chỉ khi chủ động resolve lockfile mới:

```powershell
python -m pip install -r requirements.txt `
  --extra-index-url https://download.pytorch.org/whl/cu128 `
  --extra-index-url https://pnnbao97.github.io/llama-cpp-python-v0.3.16/cpu/
python -m pip check
python -m pip freeze
```

### 1.4. Verify

```powershell
# Preflight offline: kiểm tra OS, Python, config, file model, CUDA và dependency.
.\scripts\check_environment.ps1 -SkipLlamaHealth

# Preflight trước khi live: yêu cầu llama-server /health trả status=ok.
.\scripts\check_environment.ps1

# Output machine-readable cho automation/CI local:
.\scripts\check_environment.ps1 -SkipLlamaHealth -OutputFormat Json

# CUDA + Blackwell:
python -c "import torch; print(torch.cuda.get_arch_list())"
# → phải có 'sm_120'

# nvidia-smi:
nvidia-smi
# → VRAM idle <500MB

# Test suite:
python -m pytest tests -m "not llm" --tb=short -q
# Exit code 0; không dùng số test hardcode vì suite tăng theo milestone.
```

---

## 2. Chạy hệ thống

### 2.1. Terminal 1 — llama-server (bắt buộc)

```powershell
cd E:\BAI_CUA_DUC\AI_VTUBER\v1.0
E:\BAI_CUA_DUC\llama\llama-server.exe `
  --model .\models\llm\gemma_4_12B_Q4.gguf `
  --host 127.0.0.1 --port 8080 `
  --ctx-size 4096 --n-gpu-layers 999 `
  --cache-type-k q8_0 --cache-type-v q8_0 `
  --batch-size 512 `
  --flash-attn on `
  --reasoning off
```

Chờ đến khi thấy `HTTP server is listening` (~20-25s cold load, ~10s warm cache). Verify:
```powershell
curl http://127.0.0.1:8080/health
# → {"status":"ok"}
```

### 2.2. Terminal 2 — CLI text mode (dev)

**Text chat + TTS + Autonomy tự nói + dashboard:**
```powershell
cd E:\BAI_CUA_DUC\AI_VTUBER\v1.0
venv\Scripts\Activate.ps1
python scripts\cli.py --tts --autonomy --dashboard
```

Gõ text → Enter → Mai response ra loa. Silence 60s+ → Mai tự nói.

Flags:
- `--tts` — bật VieNeu (nạp ~10-15s)
- `--autonomy` — bật Autonomy Engine v2 (Mai tự nói khi silence)
- `--dashboard` — mở http://127.0.0.1:7860
- Positional args: `python scripts\cli.py "câu 1" "câu 2"` — auto mode (mỗi arg 1 lượt, không interactive)

### 2.3. Terminal 2 — Stream mode

**YouTube live (dedicated):**
```powershell
python scripts\stream_youtube.py --video VIDEO_ID --tts --dashboard
```

**Discord (dedicated):** cần env `DISCORD_BOT_TOKEN`:
```powershell
$env:DISCORD_BOT_TOKEN = "your_bot_token_here"
python scripts\stream_discord.py --tts --dashboard
```

**Gộp YouTube + Discord:**
```powershell
# Primary YouTube:
python scripts\stream_youtube.py --video VIDEO_ID --tts --with-discord

# Hoặc primary Discord:
python scripts\stream_discord.py --tts --with-youtube VIDEO_ID
```

**Full stack (memory + dashboard + autonomy):**
```powershell
python scripts\stream_youtube.py --video VID --tts --memory --dashboard --with-discord
```

Flags stream:
- `--tts` — phát audio VieNeu
- `--memory` — semantic memory (nạp bge-m3 ~30-60s + ~2GB CPU RAM)
- `--dashboard` — realtime metrics
- `--no-autonomy` — TẮT autonomy (mặc định BẬT trong stream)
- `--with-discord` / `--with-youtube VID` — gộp source thứ 2

Ctrl+C → stop gracefully (đóng bot, cancel task, save state).

---

## 3. Setup Discord bot

Một lần cho toàn stream:

1. https://discord.com/developers/applications → **New Application**
2. Bot tab → **Reset Token** → copy
3. Bot tab → Privileged Gateway Intents → **bật MESSAGE CONTENT INTENT** (BẮT BUỘC — nếu không bot không đọc được nội dung)
4. OAuth2 → URL Generator → scopes: `bot` + permissions: `Read Messages`, `Send Messages`
5. Mở URL → invite bot vào server
6. Set env var:
   ```powershell
   $env:DISCORD_BOT_TOKEN = "your_token_here"
   ```
   Hoặc `.env` file (KHÔNG commit).
7. Copy channel ID:
   - Discord → User Settings → Advanced → **Developer Mode ON**
   - Right-click channel → Copy ID
8. Update `config/chat_sources.yaml`:
   ```yaml
   discord:
     enabled: true
     channel_ids: [123456789012345678]  # list int, empty = mọi channel bot join
   ```

---

## 4. Config đầy đủ

Toàn bộ config ở `v1.0/config/`. Nguyên tắc N6: sửa số ở YAML, KHÔNG hardcode trong .py.

### 4.1. Danh sách 15 file

| File | Vai trò | Update khi |
|---|---|---|
| `system.yaml` | Paths, event bus, resources (VRAM budget), state machine timeout, features toggle | Đổi máy / thay hardware |
| `models.yaml` | LLM (Gemma path, port, flags), TTS (VieNeu params), embedding (bge-m3) | Tune LLM/TTS latency-quality |
| `logging.yaml` | Log level, JSONL rotation size | Debug session |
| `features.yaml` | Feature toggle default state | Enable/disable memory, dashboard mặc định |
| `triggers.yaml` | 4 trigger type priority, rate limit, spam patterns, ambient threshold | Tune anti-spam |
| `state_machine.yaml` | Cooldown 500ms, interrupt policy, watchdog threshold | Debug state hang |
| `filters.yaml` | 4 filter category regex patterns | Add pattern chặn troll mới |
| `mood_engine.yaml` | Spring-damper params, baseline mood | Tune mood dynamics |
| `emotion_appraisal.yaml` | 24 category target, tone flags, modifier params | Đổi cảm xúc theo event |
| `chat_sources.yaml` | YouTube video_id, Discord token env var + channel_ids | Setup stream |
| `autonomy.yaml` | Urge params, 6 category weight/cooldown/mood_boost | Tune Mai tự nói |
| `autonomy_content_pool.yaml` | share_thought/question seed + pool policy | Đa dạng chủ đề Mai tự kể |
| `chat_salience.yaml` | base_tier, superchat_coef, tau decay, cluster, pulse thresholds | Tune ưu tiên chat + độ sôi nổi (C0) |
| `director.yaml` | segments (opening/main/chat/closing), dead_air, max_refs | Tune nhịp dẫn stream (C0) |
| `mood_style.yaml` | mood → chỉ dẫn giọng (5 chiều × 3 band × 4 trục) + inject_floor | Tune giọng theo mood (hot-reload) |

> `models.yaml.llm_main` giờ có `min_p / top_p / top_k / repeat_penalty / presence_penalty`
> (de-AI giọng, gửi thẳng vào payload llama-server). Tune ở đây nếu câu chữ còn cứng/lặp.
| `prompts/persona_system.txt` | Persona A+B+C | Đổi tính cách Mai |
| `prompts/ambient_instruction.txt` | Template Mai tự mở lời (LEGACY từ Phase 2, autonomy v2 dùng prompt_builder thay) | — |

### 4.2. Config quan trọng thường tune

**LLM latency vs quality:**
```yaml
# config/models.yaml
llm_main:
  num_predict: 500              # 300 → 500: câu dài hơn. Trade-off: TTFT kết câu +200ms
  temperature: 0.85             # 0.85 default. Cao hơn = creative. Thấp hơn = ổn định
  max_history_turns: 12         # giảm nếu prefill chậm
```

**TTS quality vs latency:**
```yaml
# config/models.yaml
tts:
  params:
    temperature: 0.75           # 0.75-0.85. Thấp = ổn định, ít vấp
    max_new_frames: 500         # 300 → 500: mỗi chunk gen dài hơn, ít gap
    top_k: 25
    style: tu_nhien             # hoặc "doc_truyen" (kể chuyện tone mượt hơn)
```

**Autonomy — Mai nói thường xuyên hơn/ít hơn:**
```yaml
# config/autonomy.yaml
autonomy:
  urge:
    urge_floor: 30.0            # ↓ (VD 20) = nói sớm hơn. ↑ = ít nói hơn
    self_cooldown_seconds: 45   # ↓ = nói liên tiếp nhanh hơn
    bon_chon_weight: 0.6        # ↑ = bon_chon mood ảnh hưởng mạnh hơn
```

**Filter aggressive/lenient:**
```yaml
# config/filters.yaml
filter:
  max_regenerate_attempts: 1    # ↑ = thử regen nhiều lần
```

---

## 5. Monitoring

### 5.1. Dashboard http://127.0.0.1:7860

Bật kèm `--dashboard`. Realtime WebSocket push mỗi 1s.

**Tabs:**
- **Metrics** — LLM (TTFT/decode/parse/fallback) + System (GPU/VRAM/chat rate) charts
- **Features** — toggle + VRAM budget
- **State Machine** — current state + duration, watchdog, transition history
- **Triggers** — type counts, skipped, interrupt (legacy path)
- **Filter** — checks/hits per category, hit rate, fail-open, regenerate outcomes
- **TTS** — TTFA per turn, chunks played/dropped, subtitle fallback
- **Mood** — chart 5 chiều realtime (pos đặc + target chấm) + tone flags. Cần
  `DashboardServer(emotion=emotion)` (stream/cli đã wire). Đây là cách trực quan nhất
  để debug "sao Mai đang gắt/buồn".

### 5.2. Logs JSONL

`logs/events.jsonl` — structlog events (INFO/WARN/ERROR):
```json
{"event": "llm_stream_ttft", "ttft_ms": 250, "request_id": "msg1", "level": "info", "timestamp": "..."}
```

`logs/turns.jsonl` — 1 record/turn:
```json
{"turn_id": 42, "user_text": "...", "mai_text": "...", "mood": {"vui": 6, ...},
 "ttft_ms": 250, "ttfa_ms": 320, "total_ms": 2500, "trigger_type": "chat_youtube"}
```

Query nhanh với `jq`:
```powershell
# TTFT P95 hôm nay:
type logs\turns.jsonl | jq -s "[.[].ttft_ms] | sort | .[.length*95/100 | floor]"

# Đếm ambient (Mai tự nói) vs chat_reply:
type logs\turns.jsonl | jq -r ".kind" | sort | uniq -c

# Kiểm A1 (target 0): số turn LLM vẫn lỡ sinh mood block:
type logs\turns.jsonl | jq "select(.raw_had_mood_block==true)" | wc -l
```

### 5.3. SQLite queries

```powershell
sqlite3 data/mai.db
```

```sql
-- Recent 20 turn:
SELECT turn_id, timestamp, trigger_type, user_name,
       substr(input_content, 1, 50) as input,
       substr(parsed_text, 1, 50) as output,
       ttfa_ms
FROM turns ORDER BY turn_id DESC LIMIT 20;

-- State transitions cuối:
SELECT timestamp, from_state, to_state, duration_in_prev_state_ms
FROM state_transitions ORDER BY transition_id DESC LIMIT 10;

-- Memory entries per viewer:
SELECT viewer_id, COUNT(*), MAX(timestamp)
FROM memory_entries GROUP BY viewer_id ORDER BY COUNT(*) DESC;

-- Vector index size:
SELECT COUNT(*) FROM memory_vectors;
```

---

## 6. Emergency stop

**Ctrl+Shift+X từ bất kỳ đâu** (cần chạy Python admin). → StateMachine PAUSED → clear queue → cancel current turn.

Resume: dashboard REST `POST /resume` hoặc restart process.

Nếu không admin, hotkey không active (degrade). Fallback: Ctrl+C terminal → graceful shutdown.

---

## 7. Test suite

### 7.1. Chạy full

```powershell
cd E:\BAI_CUA_DUC\AI_VTUBER\v1.0
venv\Scripts\Activate.ps1
python -m pytest tests/ --tb=short -q `
  -m "not llm"
# → exit code 0
```

Marker `not llm` bỏ các test cần llama-server thật. Chạy live test riêng:
```powershell
# Với llama-server đang chạy:
python -m pytest tests/integration/test_llama_server_live.py -v
```

### 7.2. Chạy per-phase

```powershell
python scripts\test_phases.py 4     # Phase 4 TTS only
python scripts\test_phases.py 7 7.5 # Phase 7 + 7.5
```

### 7.3. Chạy 1 test cụ thể

```powershell
python -m pytest tests/unit/test_mood_engine.py::TestStability10k::test_10k_ticks_no_nan -v
```

---

## 8. Troubleshooting

### 8.1. llama-server crash / không chạy

**Symptom:** `❌ llama-server chưa chạy: ...`

**Check:**
1. Terminal 1 llama-server có chạy không? `curl http://127.0.0.1:8080/health`
2. Model path đúng? Path relative `.\models\llm\gemma_4_12B_Q4.gguf` — chạy từ `v1.0/`, không phải `AI_VTUBER/`
3. VRAM đủ? `nvidia-smi` → cần free ≥8GB (Gemma 12B Q4)

### 8.2. VieNeu-TTS crash

**Symptom (a) — torchcodec DLL:** `Could not load libtorchcodec_core8.dll`

Đã có patch trong `vieneu_service.py` (bypass torchcodec, dùng soundfile). Nếu bypass patch không hoạt động → cài FFmpeg 7 full-shared:
1. Tải https://github.com/BtbN/FFmpeg-Builds/releases → `ffmpeg-master-latest-win64-gpl-shared.zip`
2. Extract to `C:\ffmpeg`, add `C:\ffmpeg\bin` vào PATH
3. Restart terminal

**Symptom (b) — CUDA sm_120 kernel:** `no kernel image available for execution on the device`

torch không phải cu128 hoặc quá cũ. Fix:
```powershell
pip uninstall -y torch torchaudio
pip install torch==2.11.0+cu128 torchaudio==2.11.0+cu128 `
  --extra-index-url https://download.pytorch.org/whl/cu128
```

**Symptom (c) — TTFA 5000ms:** Quên `add_voice()` trong service init. Check `vieneu_service.py.start()` phải gọi `_enroll_reference()`.

### 8.3. Autonomy không tự nói

**Check dashboard urge:**
- Urge < `urge_floor` (30) → chưa đủ, đợi thêm
- Urge > 30 nhưng vẫn không nói → probabilistic (sigmoid ~50% quanh 50). Đợi vài tick.

**Force test:**
```yaml
# config/autonomy.yaml — TẠM cho test:
autonomy:
  urge:
    urge_floor: 5.0             # ↓ threshold
    self_cooldown_seconds: 10   # ↓ cooldown
    rise_base: 2.0              # ↑ rise nhanh
```

Restart CLI với `--autonomy` — Mai nên tự nói sau ~15s silence.

### 8.4. Discord bot không nhận message

1. Bot online trên Discord (green dot)?
2. **MESSAGE CONTENT INTENT** đã bật ở Developer Portal? (BẮT BUỘC)
3. Bot có permission Read Messages trong channel không?
4. `channel_ids` config đúng không? (test với empty `[]` để accept mọi channel)
5. Env `DISCORD_BOT_TOKEN` set đúng? `echo $env:DISCORD_BOT_TOKEN`

### 8.5. YouTube chat không stream

`pytchat` chỉ scrape **public** live stream. Video ID lấy từ URL `youtube.com/watch?v=<ID>`.

Nếu 404 → stream đã end hoặc video không live. Nếu rate limit → tăng `poll_interval_s` (2 → 5).

### 8.6. Memory retrieve chậm

**Symptom:** `memory_query_timeout` warning trong log.

- bge-m3 chưa warm — first query chậm do load. Sau đó P95 <150ms.
- CPU load cao? Memory chạy CPU, nếu llama-server đang decode nặng có thể contention.
- Fix: `--memory` off nếu không cần, hoặc tăng timeout (config models.yaml.memory.query_timeout_s tăng nếu cần).

### 8.7. Test suite fail

**Common:**
- `test_reads_config` fail: config yaml sai key hoặc value. Check gợi ý error diff.
- `test_migration_runner` flaky (rare): 2 migration cùng giây đè backup. Xoá `backups/` rồi rerun.
- Live LLM test skip: bình thường nếu không có llama-server chạy.

---

## 9. Backup + restore

### 9.1. Auto backup

MigrationRunner tự backup `data/mai.db` trước MỖI migration → `backups/mai.db.pre_migration_<ts>_<micro>`.

Không có scheduled backup — chỉ pre-migration. Setup cron tự backup nếu cần data lâu dài.

### 9.2. Restore từ backup

```powershell
# Stop hệ thống, replace file:
Stop-Process -Name python -Force  # careful!
Copy-Item backups\mai.db.pre_migration_20260805_143012_912345 data\mai.db
# Restart llama-server + stream
```

### 9.3. Xoá memory sạch

```powershell
Remove-Item data\mai.db
# Restart — MigrationRunner tự tạo lại schema
```

---

## 10. Performance tuning checklist

Khi live thấy chậm, check thứ tự:

1. **TTFT LLM > 1s?** Kiểm tra: llama-server đã cache prefix chưa (turn 2+ warm). Nếu vẫn chậm → `max_history_turns` giảm 12→8.
2. **TTFA TTS > 500ms?** Kiểm tra `add_voice()` cache. Log `[TTS] svc_TTFA=` — nếu 5000ms thì chưa cache.
3. **first-sentence-at > 2s?** Kiểm tra `on_token` streaming vào `LiveSentenceStreamer` — TTS phải bắt đầu synth ngay khi thấy dấu chấm đầu.
4. **RTF > 1.0?** GPU nghẽn (LLM + VieNeu đồng thời). Giảm `num_predict` hoặc tách 2 GPU (chưa spec).
5. **Memory query > 150ms?** bge-m3 CPU contention. Chấp nhận (fail-safe trả []). Hoặc tắt `--memory`.
6. **Autonomy đè chat turn?** Turn_lock đang giữ. Không thể — chỉ 1 turn tại 1 lúc là design intent.

---

## 11. Roadmap chưa làm

- **Phase 5 STT** — voice input (faster-whisper small). Deferred cuối MVP theo memory user.
- **Phase 6 Animation** — VTube Studio API, model 2D VRM/VSFAvatar. Mai có mặt biểu cảm.
- **Phase 8 QC + Data pipeline** — chấm output persona rubric, export JSONL Unsloth.
- **Phase 9 Fine-tune** — SFT/DPO qua Unsloth, Mai v2.

**Live checkpoints treo (không code, chỉ user chạy verify):**
- P4 TTS quality subjective ≥6/10 qua 30 câu
- P7 Memory: bge-m3 thật, retrieve P95 <150ms live
- P7.5 Emotion: 100 turn mood curve "cảm thấy đúng"
- Autonomy v2: 2h live subjective, mood curve tự nhiên
- Platform: unlisted YouTube live end-to-end
