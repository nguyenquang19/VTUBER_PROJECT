# 01 — Kiến trúc & Data Flow

> Canonical. Phản ánh code THẬT tại 2026-08-06 (sau A1 + C0 Director). Nếu doc lệch
> code, code đúng — sửa doc.

## 1. Mai là gì

AI VTuber tiếng Việt, chạy 100% local trên Windows 11 (RTX 5060 Ti 16GB). Đọc chat
YouTube + Discord → **Director** quyết định nên nói gì → sinh câu qua LLM (Gemma 4 12B Q4)
→ phát audio qua VieNeu-TTS v3 Turbo (48kHz). Có mood engine (spring-damper), memory
persistent (SQLite+vec), và autonomy engine cho Mai tự nói khi vắng.

Persona (`config/prompts/persona_system.txt`): nữ 18t, xưng "tớ" / gọi chat "cậu" / gọi
operator "ông", biết là AI nhưng ghét bị nhắc. Không hedge, bị chọc thì cà khịa lại.
**A1 (2026-08): Mai CHỈ xuất thoại — không còn kê khai mood block `[vui:N…]`.** Mood do
engine tự tính từ sự kiện (Kênh A), không lấy từ LLM self-report nữa.

---

## 2. Nguyên tắc bất biến (N1-N8)

Từ `docs/CLAUDE.md`. Vi phạm = dừng, hỏi lại.

| # | Nguyên tắc | Áp dụng |
|---|---|---|
| N1 | YAGNI — đúng số spec | 5 mood dim, 5 director action — không phình |
| N2 | Persona ranh giới Phần C CỨNG | Không "làm nhẹ" dù user yêu cầu |
| N3 | Không skip Pre-flight | Spike report trước khi commit (VD B-spike vision) |
| N4 | DoD mỗi phase tick trước khi qua | `STATE.md` tracking |
| N5 | Test theo phase | Mỗi module có unit test riêng |
| N6 | Config over code | Số ở YAML (`config/*.yaml`), không hardcode |
| N7 | Filter fail-open, phần khác fail-safe | Module lỗi → fallback, không sập hệ |
| N8 | Interface-based | `interfaces/*.py` ABC, implement mới không sửa caller |

---

## 3. Kiến trúc layer

```
LAYER 6 — Scripts / entrypoint
  scripts/cli.py · scripts/stream_youtube.py · scripts/stream_discord.py

LAYER 5 — Runtime composer + DRIVER
  orchestrator/stream_runtime.py  — compose full stack + build_stream_runtime()
  services/director/director_loop.py — DRIVER DUY NHẤT (cầm nhịp, sinh mọi turn)
  services/input/chat_router.py   — intake: bơm chat vào pool/pulse (KHÔNG tự đáp)

LAYER 4 — Services
  LLM stack (P1) · TTS pipeline (P4) · Emotion Orchestrator (P7.5)
  Memory Fallback (P7) · Autonomy Engine v2 (Aut) · Director stack (C0)

LAYER 3 — Interfaces (interfaces/*.py ABC)
  LLMService · TTSService · MemoryService · MoodState · FilterService · InputService

LAYER 2 — Foundation (Phase 0)
  ConfigLoader · Logger · EventBus · StateMachine · FallbackManager
  MigrationRunner · MetricsCollector · EmergencyStop · DashboardServer

LAYER 1 — External processes
  llama-server.exe (port 8080, Gemma 4 12B Q4) · SQLite + sqlite-vec
  YouTube/Discord API (pytchat / discord.py)
```

**Thay đổi lớn so với bản cũ:** trước đây ChatRouter tự đáp mọi chat theo FIFO. Giờ
**DirectorLoop là driver duy nhất** — ChatRouter chỉ *bơm* chat vào SaliencePool + ChatPulse
(intake mode), Director quyết khi nào/đáp tin nào. (Đường FIFO cũ vẫn còn trong ChatRouter
làm fallback khi không cấp pool/pulse — dùng cho test.)

---

## 4. Hai vòng lặp nền

Hệ chạy 2 background loop, cùng chia 1 `turn_lock` (asyncio.Lock) → không bao giờ 2 turn
LLM song song (llama-server 1 instance):

1. **ChatRouter consumers** (1 task/nguồn) — nhận event chat → appraisal mood → **bơm vào
   pool/pulse**. KHÔNG sinh turn.
2. **DirectorLoop** (`director_loop.py`) — tick mỗi `tick_seconds` → `Director.decide()` →
   thực thi action (đáp chat / tự nói / chuyển segment) qua `turn_lock`.

Emotion còn 1 tick loop riêng 10Hz (spring-damper mood), độc lập, không sinh turn.

---

## 5. Vòng đời 1 lượt "đáp chat" — end-to-end

Ví dụ: chat "Mai giỏi quá đi" từ YouTube.

### Bước 1 — Ingest
```
pytchat poll → YouTubeChatService._to_event() → InputEvent(
  source=CHAT_YOUTUBE, content="Mai giỏi quá đi", user_id="UCxxx", user_name="Alice")
→ event_stream()
```

### Bước 2 — ChatRouter intake (KHÔNG đáp)
```
ChatRouter._consume → _process(event) [đã bọc hook: autonomy.on_external_activity()
  + rt.note_chat_activity()]:
  1. emotion.handle_event(_to_emotion_event(ev))  → cập nhật mood (appraisal, xem Bước 3)
  2. _pump_intake(ev):
       pool.add(msg_id, text, now, kind=chat|question|mention, viewer_id, amount_vnd, is_super)
       pulse.record(now, user_id)
  → KẾT THÚC. Không sinh turn. Tin nằm trong SaliencePool chờ Director nhặt.
```

### Bước 3 — Emotion (chạy song song, không chặn)
```
EmotionOrchestrator.handle_event:
  T1 EventClassifier → "chat_compliment"
  T2 AppraisalTable → {vui:7, nguong:6}; ModifierEngine (first_time ×1.2) → {vui:8.4,...}
  Buffer pending per-dim; ghi active_cause (A4: "Alice khen")
Tick loop 10Hz: flush_and_tick → saturate → MoodEngine.apply_appraisal → tick(0.1)
  → position bò từ baseline về target (spring-damper).
```
Mood là **1 chiều**: sự kiện → engine → prompt. LLM KHÔNG report ngược (A1 bỏ Kênh B).

### Bước 4 — DirectorLoop nhặt & quyết
```
DirectorLoop.tick_once (mỗi tick_seconds, skip nếu turn_lock đang giữ):
  pool.evict_stale(now)                    # tin decay < floor → xoá
  urge_ready = autonomy.urge.should_speak_now()
  dec = Director.decide(now, urge_ready):
     - hết giờ segment → TRANSITION
     - top.is_super & segment cho ack → ACK_DONATION (chen hàng)
     - pulse HYPE_SPAM → READ_CHAT mode VIBE
     - có top & chưa đáp chat liên tiếp quá max → READ_CHAT (single/cluster/summary)
     - chat nguội / dead-air / bị ép xen / urge → SELF_TALK (hoặc TRANSITION)
     - else WAIT
  if WAIT: return
  async with turn_lock: execute(dec)
```

### Bước 5 — Execute READ_CHAT → LLM turn
```
DirectorLoop._exec_read:
  user_text = _compose_read_prompt(dec)   # theo read_mode: single=text gốc,
                                          # cluster="[mấy người hỏi:…]", summary/vibe=chỉ thị
  parsed,_ = runner.run_turn(user_text, viewer_id=refs[0].viewer_id, trigger_type="director_read")
      LLMTurnRunner.run_turn:
        _build_request_maybe_with_mood → PromptManager.build_request_with_mood:
          [persona cache] + [Context: current_mood + cause + tone_flags] + history + [user]
        FallbackManager.execute("llm"): L0 LlamaCpp stream (raw socket, cache_prompt)
                                        L1 CannedResponder (nếu timeout)
        parser.parse_response → text THUẦN (A1: mood block nếu lỡ có thì strip, không dùng)
        pm.commit_turn(user_text, parsed.text)   # history, trim 12 turn
        _apply_emotion_feedback → CHỈ clear_tone_flags (Kênh B + drift đã bỏ)
        _schedule_memory_write → MemoryExtractor → asyncio.create_task(memory.write)  # bg
  pool.remove(refs)                        # gỡ tin đã đáp
  director.mark_spoke(READ_CHAT)           # đếm consecutive_read_chat
  await speak(parsed.text)
```

### Bước 6 — Speak (nếu TTS)
```
TTSPipeline.speak: split_vn → mỗi câu FallbackManager("tts"):
  L0 VieNeuTtsService.synthesize_stream(voice="mai_ref" đã enroll) → AudioChunk stream
  L1 SubtitleFallback (nếu lỗi)
AudioPlayer worker: play chunk tuần tự (no overlap). Câu N+1 gen song song khi phát câu N.
```

**Tổng warm:** chat arrive → Director nhặt (≤ tick_seconds) → first audio ≈ 1-2s sau khi nhặt.

---

## 6. Vòng đời 1 lượt "tự nói" (SELF_TALK)

Khi Director chọn SELF_TALK (dead-air / chat nguội / urge / bị ép xen):
```
DirectorLoop._exec_self_talk:
  decision = autonomy.force_generate(mood, ctx):     # KHÁC maybe_generate: bỏ qua prob gate
     selector.select(cat) → material.get(cat,ctx) → render_prompt(seed, forbidden_openers…)
     None nếu mọi cat thiếu material
  parsed = runner.run_ambient_turn(decision.prompt_text)   # KHÔNG commit history
  if autonomy.check_dedup(parsed.text): regen 1 lần
  autonomy.on_self_spoke(text): urge=0 + cooldown 45s + opener.record + dedup.record
  runner.commit_self_talk(text)                      # đẩy vào history cho continuity
  director.mark_spoke(SELF_TALK); await speak
```
Autonomy engine (Urge/Selector/Material/Opener/Dedup) chi tiết ở `02_modules.md §Autonomy`.

> Lưu ý: `StreamRuntime._autonomy_loop()` (bản cũ tự cầm nhịp) chỉ chạy khi KHÔNG có
> DirectorLoop (backward-compat/test). Ở stream thật, Director cầm nhịp.

---

## 7. State Machine 5 state (Phase 0)

```
IDLE ─trigger─► LISTENING ─analyze─► THINKING ─stream─► SPEAKING ─done─► IDLE
                                              PAUSED ◄─ Ctrl+Shift+X (từ mọi state)
```
Cooldown 500ms giữa transition. Watchdog 5s: quá `max_time_in_state` (`config/state_machine.yaml`)
→ emergency_stop. File: `orchestrator/state_machine.py`, `state_watchdog.py`, `emergency_stop.py`.

> State machine + TriggerManager/TurnOrchestrator (`orchestrator/`) là đường điều phối
> CŨ, dùng bởi `main.py`/`cli.py` không-director. Đường **stream** (director) không đi qua
> chúng. Xem `04_extending.md §Hai đường điều phối`.

---

## 8. Fallback Chain (N7 fail-safe)

| Module | L0 primary | L1 backup | L2 |
|---|---|---|---|
| LLM | LlamaCppLLMService (stream) | CannedResponder (câu preset theo mood) | — |
| Filter | RuleFilter (regex 4 category) | — | fail-open (cho qua + log) |
| TTS | VieNeuTtsService (streaming) | SubtitleFallback (text) | — |
| Memory | SemanticMemoryService (150ms timeout) | WorkingMemoryService (deque 20) | — |

Timeout ở `config/models.yaml`: `llm_canned.timeout_primary_s: 5.0` / `timeout_canned_s: 0.1`;
memory hard timeout 0.15s.

---

## 9. Serialization

**1 turn LLM tại 1 thời điểm.** Enforce qua `turn_lock` chung giữa ChatRouter intake và
DirectorLoop. `AudioPlayer` worker queue → chunk N+1 chỉ chạy khi N xong.

Backpressure chat: chat dồn → nằm trong SaliencePool, tin điểm thấp/cũ **tự decay & evict**
(không xếp hàng vô hạn như FIFO cũ). Xem `02_modules.md §Director`.

---

## 10. Tech stack chốt cứng

| Component | Chốt | Ràng buộc |
|---|---|---|
| OS | Windows 11 | Path `\`, no SIGTERM |
| Python | 3.11+ | async/await |
| LLM | llama.cpp (llama-server) | KHÔNG Ollama/vllm |
| Model | Gemma 4 12B Q4_K_M | port 8080, --reasoning off |
| CUDA / torch | 12.8+ / 2.11.0+cu128 | Blackwell sm_120 |
| TTS | VieNeu-TTS v3 Turbo | 48kHz GPU streaming |
| Embedder | BAAI/bge-m3 CPU | dim 1024 |
| DB | SQLite + sqlite-vec | `data/mai.db` |
| Web | FastAPI + Vanilla JS | KHÔNG React |

Đổi tech → phải có spike report. Đã đổi: viXTTS → VieNeu (spike `spike/day_vieneu/`).

---

## 11. Đọc tiếp
- `02_modules.md` — logic từng module (LLM/TTS/Memory/Emotion/Autonomy/**Director**/Filter/Platform)
- `03_operations.md` — chạy, config, dashboard (có tab Mood), test, troubleshoot
- `04_extending.md` — thêm module/nguồn mới, hai đường điều phối
