# TÀI LIỆU KỸ THUẬT DỰ ÁN MAI - ARCHITECTURE

**Phiên bản:** 2.2 (đã merge đầy đủ — đây là bản DUY NHẤT cần đọc, không cần ghép thêm file nào khác)
**Ngày cập nhật:** 2026-07-27
**Đối tượng:** Developer solo
**Mục tiêu:** AI VTuber tiếng Việt, 100% local, kiến trúc feature-toggle với dashboard giám sát
**Target OS:** Windows 11 (single target)
**LLM backend:** llama.cpp (llama-server), không dùng Ollama

**File đi kèm:** `QUICKSTART.md` (đọc trước file này — chỉ đường đến phần cần đọc).

---

**Lịch sử phiên bản:**

**v1.0 → v2.0:**
- Thêm Section 0: Pre-flight Validation (spike days bắt buộc)
- Thêm Section 7.5: Trigger & Turn-Taking Logic
- Thêm Section 7.6: Conversation State Machine chi tiết
- Thêm Section 8.7: Fallback Chains cho mỗi module
- Điều chỉnh latency target: P50/P95 thay vì cứng 1.5s
- Whisper base → faster-whisper small
- Timeline x1.3 với "Definition of Done" cụ thể
- Thêm Appendix D: Pre-flight Checklist
- Thêm Section 13.7: Component Crash Recovery

**v2.0 → v2.1:**
- ⚠️ **Fix bug:** context_size 2048 (v2.0) → revert 4096, vì 2048 không đủ chứa persona+memory+system (~2800 token tối thiểu)
- Simplify Trigger types: 12 → 4 (YAGNI, grow-when-needed)
- Simplify States: 8 → 5, transitions: 18 → 9
- Simplify Fallback chains: 3-4 levels → 2 levels, bỏ circuit breaker ban đầu
- Thêm Section 4.6: Graceful Degradation Policy
- Thêm Section 8.8: Data Migration Strategy
- Thêm Section 12.8: Trigger + State Integration Tests
- Thêm Section 2.3: Common Pitfalls cho solo dev
- Tách `QUICKSTART.md` ra file riêng
- **Complexity giảm ~40% so với v2.0 mà không mất chức năng cốt lõi**

**v2.1 → v2.2 (bản hiện tại):**
- **Chốt LLM backend:** llama.cpp (llama-server) làm chính, không dùng Ollama — control trực tiếp KV cache Q8, speculative decoding, prompt caching
- **Chốt OS target:** Windows 11 duy nhất (bỏ dual-support Linux) — VTube Studio native, ecosystem VTuber
- Cập nhật toàn bộ code/script/path sang Windows convention (PowerShell, `\` separator, `.exe` extension, `TerminateProcess()` thay SIGTERM)
- Thêm Windows-specific notes: execution policy, long path, hotkey admin, AV exception
- Fix bug VRAM: `llm_fallback` và `llm_filter` giờ dùng chung 1 `llama-server` instance (port 8082), tiết kiệm 3GB
- Khôi phục các section bị mất khi merge (7.1-7.4 Base/Input/STT/LLM interface, 7.5-7.8 Filter/TTS/Animation/Memory interface, 8.1-8.6 module specs, 13.1-13.6 deployment)
- Renumber Section 7: Trigger → 7.9, State Machine → 7.10

---

## MỤC LỤC

0. [Pre-flight Validation](#0-pre-flight-validation) ⚡ **BẮT BUỘC làm trước Phase 0**
1. [Tổng quan hệ thống](#1-tổng-quan-hệ-thống)
2. [Nguyên tắc thiết kế](#2-nguyên-tắc-thiết-kế)
3. [Kiến trúc tổng thể](#3-kiến-trúc-tổng-thể)
4. [Feature Toggle System](#4-feature-toggle-system)
5. [Monitoring & Metrics](#5-monitoring--metrics)
6. [Dashboard Specification](#6-dashboard-specification)
7. [Interface Definitions](#7-interface-definitions)
   - 7.1-7.4. Base, Input, STT, LLM interfaces
   - 7.5-7.8. Filter, TTS, Animation, Memory interfaces
   - 7.9. [Trigger & Turn-Taking Logic](#79-trigger--turn-taking-logic) ⭐
   - 7.10. [Conversation State Machine](#710-conversation-state-machine) ⭐
8. [Module Specifications](#8-module-specifications)
   - 8.1-8.6. Orchestrator, LLM, Filter, TTS, Animation, Memory modules
   - 8.7. [Fallback Chains](#87-fallback-chains) ⭐
   - 8.8. [Data Migration Strategy](#88-data-migration-strategy) ⭐
9. [Data Schema](#9-data-schema)
10. [Configuration](#10-configuration)
11. [Development Roadmap](#11-development-roadmap)
12. [Testing Strategy](#12-testing-strategy)
13. [Deployment & Operations](#13-deployment--operations)
14. [Appendix A: File Structure](#appendix-a-file-structure)
15. [Appendix B: Khởi động nhanh](#appendix-b-khởi-động-nhanh)
16. [Appendix C: Trade-off Decisions Log](#appendix-c-trade-off-decisions-log)
17. [Appendix D: Pre-flight Checklist](#appendix-d-pre-flight-checklist) ⭐ **MỚI**

---

## 0. PRE-FLIGHT VALIDATION

⚡ **CRITICAL: Không skip section này. Đây là 2-3 ngày spike để validate assumptions trước khi commit vào architecture.**

### 0.1. Tại sao cần

Architecture của v1.0 dựa trên nhiều assumptions chưa được test:
- Piper có voice tiếng Việt đủ tốt?
- Gemma 12B trên RTX 5060 Ti đạt TTFT < 1s không?
- Whisper accuracy tiếng Việt bao nhiêu %?
- KV cache Q8 quantization có ảnh hưởng chất lượng persona?

Nếu 1 trong 4 assumption sai → phải re-architect. Thà mất 3 ngày spike còn hơn mất 3 tuần code rồi throw away.

### 0.2. Spike Day 1: LLM Latency Benchmark

**Goal:** Confirm Gemma 12B trên hardware của bạn đạt latency target.

**Setup:**
```powershell
# Đã có llama.cpp sẵn — build lại bản mới nhất nếu cần (CUDA support)
cd llama.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j

# Tải model GGUF (Gemma 4 12B, Q4_K_M)
huggingface-cli download <repo>/gemma-4-12b-GGUF gemma-4-12b-Q4_K_M.gguf `
  --local-dir .\models

# Chạy server (OpenAI-compatible endpoint)
# Lưu ý: build MSVC/Visual Studio thường đặt exe tại build\bin\Release\, không phải build\bin\
.\build\bin\Release\llama-server.exe `
  -m .\models\gemma-4-12b-Q4_K_M.gguf `
  -ngl 999 `
  -c 4096 `
  --cache-type-k q8_0 --cache-type-v q8_0 `
  --port 8080

# Prepare benchmark
git clone <this-repo>
cd mai\spike\day1_llm_latency
python benchmark.py --endpoint http://localhost:8080
```

**Test scenarios:**

| Scenario | Context Size | Expected TTFT | Expected Decode |
|---|---|---|---|
| Cold start | 500 tokens | < 500ms | > 50 tok/s |
| Warm short | 500 tokens (cached) | < 300ms | > 60 tok/s |
| Warm medium | 2K tokens | < 800ms | > 45 tok/s |
| Warm long | 4K tokens | < 1500ms | > 35 tok/s |
| Overheating (30 min chạy liên tục) | 2K tokens | < 1000ms | > 40 tok/s |

**Deliverable:** File `spike/day1_report.md` với:
- Bảng số thực tế đo được
- So sánh với target
- **Go/No-go decision** cho architecture

**No-go criteria:**
- TTFT cold > 1s → architecture cần adjust (chuyển sang model nhỏ hơn hoặc dual-model)
- Decode speed < 30 tok/s → cân nhắc E4B thay 12B cho main LLM
- Overheating throttle > 30% → cần thermal management plan

### 0.3. Spike Day 2: TTS Vietnamese Quality

**Goal:** Confirm có TTS Vietnamese đủ tốt cho VTuber.

**Test candidates (theo thứ tự ưu tiên):**

1. **Piper voices**
   - Download all Piper Vietnamese voices từ Hugging Face
   - Synthesize 10 mẫu câu (chào hỏi, cảm xúc khác nhau, câu dài)
   - Đánh giá subjective: naturalness, prosody, emotion capability

2. **XTTS v2 với Vietnamese**
   - Setup XTTS v2 local
   - Same 10 mẫu câu
   - So sánh chất lượng, đo latency, VRAM

3. **viXTTS (nếu tìm được)**
   - Vietnamese-tuned XTTS
   - Test tương tự

4. **F5-TTS**
   - Setup F5-TTS
   - Test khả năng Vietnamese

**Decision matrix:**

| TTS | Quality (1-10) | Latency | VRAM | Emotion | Decision |
|---|---|---|---|---|---|
| Piper | ? | ? | 0 | No | ? |
| XTTS v2 | ? | ? | 2-3GB | Yes | ? |
| viXTTS | ? | ? | 2-3GB | Yes | ? |
| F5-TTS | ? | ? | 1.5-2GB | Partial | ? |

**Go criteria:**
- Có ít nhất 1 option đạt quality ≥ 6/10 với latency < 800ms

**No-go scenarios:**
- Nếu tất cả < 6/10 → phải cân nhắc:
  - Chấp nhận chất lượng thấp cho v1 (Piper), upgrade sau
  - Hoặc phá "100% local" constraint, dùng edge-tts (Microsoft, free, cần network)
  - Hoặc invest fine-tune TTS model riêng (advanced, tốn thời gian)

**Deliverable:** `spike/day2_report.md` với:
- Audio samples (attached)
- Bảng so sánh
- **Chosen TTS + fallback plan**

### 0.4. Spike Day 3: STT Vietnamese Accuracy

**Goal:** Confirm STT đủ chính xác cho conversation.

**Test candidates:**

1. **faster-whisper small (Vietnamese)** — primary
2. **faster-whisper medium** — nếu VRAM cho phép
3. **Whisper.cpp** — CPU fallback

**Test set:**
- 20 câu tự nói vào mic (giọng bạn/operator)
- 10 câu tự nhiên (không đọc từ script)
- 5 câu có noise nền (music, fan)
- 5 câu có dấu câu phức tạp

**Metrics:**
- WER (Word Error Rate) tổng
- WER cho từng loại (script vs natural vs noisy)
- Latency per chunk
- VRAM footprint

**Go criteria:**
- WER < 15% cho câu script
- WER < 25% cho câu tự nhiên
- Latency chunk < 300ms

**No-go:** WER > 30% cho câu tự nhiên → conversation không dùng được, cần plan khác (như PTT - push to talk, hoặc restrict câu ngắn).

**Deliverable:** `spike/day3_report.md` với recommendation.

### 0.5. Spike Day 4 (optional): End-to-End Latency Estimate

**Goal:** Confirm total pipeline < target với real components.

**Setup:** Ghép Whisper + Gemma + TTS đã chọn thành mini pipeline (chưa filter, chưa animation).

**Test:** 30 turns real conversation.

**Measure:**
- Total latency: chat/voice input → audio playback start
- Breakdown per stage
- P50, P95, P99

**Adjust target dựa vào kết quả thực tế.**

### 0.6. Kết quả từ Pre-flight

Sau 3-4 ngày spike, cập nhật:
- `Appendix C` (Trade-off log) với decisions mới
- `Section 1.1` (latency target) với number thực tế
- `Section 10.3` (models.yaml) với model đã chọn

**Chỉ tiến hành Phase 0 sau khi hoàn tất pre-flight.**

---

## 1. TỔNG QUAN HỆ THỐNG

### 1.1. Mục tiêu

Xây dựng AI VTuber tên **Mai** với các đặc điểm:
- Nói tiếng Việt tự nhiên với persona đã định (xem file `persona.md`)
- **Latency targets (v2 - realistic):**
  - **Time to First Audio (TTFA):** P50 < 1s, P95 < 1.8s ⭐ (metric quan trọng nhất)
  - **Total turn latency:** P50 < 1.5s, P95 < 2.5s
  - **TTFT LLM:** P50 < 700ms, P95 < 1.2s
- Chạy 100% local, không phụ thuộc cloud services (có exception documented)
- Mọi feature có thể bật/tắt runtime qua dashboard
- Mọi metric (VRAM, latency, throughput) hiển thị realtime

**Note về TTFA:** Đây là metric UX quan trọng hơn total latency. Người xem cảm nhận Mai "nhanh" khi bắt đầu nghe tiếng, không phải khi câu nói xong.

### 1.2. Constraints

- **Hardware**: RTX 5060 Ti 16GB (single machine, dev phase)
- **Budget**: $0 (100% free/open-source, với exception nếu Pre-flight không tìm được TTS local đủ tốt)
- **Kiến trúc**: Modular, có thể tách máy sau (interface-based)
- **Ngôn ngữ**: Python 3.11+ (main), có thể mix C++/Rust cho performance-critical

### 1.3. Scope

**In scope:**
- Chat từ Twitch/YouTube
- Voice input từ operator ("ông")
- LLM inference với persona
- Filter output
- TTS + Live2D animation
- Memory system (hierarchical)
- Dashboard với feature toggle + metrics
- **Trigger & Turn-Taking logic**
- **Ambient talk (Mai tự nói khi im lặng)**

**Out of scope (Phase 1):**
- Multi-language support
- Game playing AI
- Singing capability
- Multi-agent (Evil Mai variant)
- Cloud deployment

---

## 2. NGUYÊN TẮC THIẾT KẾ

### 2.1. Core principles

**P1. Feature Toggle First**
Mọi feature có thể bật/tắt runtime không cần restart. Không có "hardcoded features".

**P2. Observable by Design**
Mọi component expose metrics. Không component nào là black box.

**P3. Interface-based**
Modules communicate qua interface trừu tượng. Chuẩn bị cho việc tách máy sau.

**P4. Fail-safe defaults**
Nếu feature fail → fallback về behavior cơ bản. Không crash hệ thống.

**P5. Config over code**
Behavior thay đổi qua config file/dashboard, không phải sửa code.

**P6. Neuro-inspired simplicity**
Không over-engineer. Nếu Neuro không cần → mặc định Mai cũng không cần.

**P7. Validate Before Build** ⭐ **MỚI**
Không commit vào architecture decision quan trọng nếu chưa validate assumption. Spike days có ROI cao hơn refactor.

**P8. Every Module Has a Fallback** ⭐ **MỚI**
Mỗi module trong critical path phải có ít nhất 1 fallback. Không có single point of failure.

### 2.2. Anti-patterns cần tránh

- ❌ Hardcode magic numbers trong code
- ❌ Feature không có toggle
- ❌ Feature không có metric
- ❌ Direct call giữa modules không qua interface
- ❌ Global state không có access control
- ❌ Silent failure (fail phải log rõ ràng)
- ❌ Commit vào assumption chưa validate
- ❌ Không có fallback plan cho critical component
- ❌ Build feature phức tạp trước khi có bản đơn giản chạy được (vi phạm P6) ⭐ **MỚI (v2.1)**

### 2.3. Common pitfalls cho solo dev ⭐ **MỚI (v2.1)**

Rút ra sau nhiều vòng review — các pitfall này lặp lại và đáng cảnh báo trước:

**Pitfall 1: Over-planning, under-executing**
Symptom: đọc doc hàng tuần, chưa code dòng nào. Fix: đọc `QUICKSTART.md` + Section 0, bắt tay Pre-flight ngay. Học qua làm, không qua đọc.

**Pitfall 2: Skip Pre-flight**
Symptom: "sẽ validate sau", commit thẳng vào architecture. Fix: 4 ngày spike có ROI cao hơn nhiều so với 4 tuần refactor sau này.

**Pitfall 3: Over-engineer từ Phase 0**
Symptom: build Trigger 12 types trước khi có bản 4 types chạy ổn. Fix: YAGNI nghiêm ngặt — bắt đầu tối giản (xem Section 7.9, 7.10 đã simplify), grow khi hit giới hạn thật.

**Pitfall 4: Tune persona quá sớm**
Symptom: Phase 1 chưa xong đã sửa persona 20 lần. Fix: chạy đủ 100+ turn rồi mới tune. Persona v1 sẽ tệ — đó là expected, không phải lý do để sửa liên tục.

**Pitfall 5: Coi fine-tune như silver bullet**
Symptom: gặp vấn đề gì cũng nghĩ "fine-tune sẽ giải quyết". Fix: fine-tune là Phase 9 (cuối cùng) vì lý do — prompt engineering trước, memory sau, fine-tune cuối.

**Pitfall 6: Build hết feature rồi mới test**
Symptom: xong Phase 4 mới test → dính hàng loạt integration bug dồn cục. Fix: mỗi Phase có Definition of Done bao gồm test — test theo phase, không tích luỹ nợ.

**Pitfall 7: Deadline pressure đánh đổi chất lượng**
Symptom: "chỉ cần deliver, quality tính sau". Fix: VTuber là marathon — chất lượng kém khiến viewer bỏ đi không quay lại. Chậm mà chắc tốt hơn nhanh mà vỡ.

---

## 3. KIẾN TRÚC TỔNG THỂ

### 3.1. High-level diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     DASHBOARD (Web UI)                       │
│  Toggles │ Metrics │ Logs │ Persona Editor │ Emergency Stop  │
└───────────────────────┬─────────────────────────────────────┘
                        │ (WebSocket bidirectional)
                        ↓
┌─────────────────────────────────────────────────────────────┐
│                    ORCHESTRATOR (main.py)                    │
│  - Feature registry                                          │
│  - Metrics collector                                         │
│  - Event bus (internal asyncio)                              │
│  - Health monitor                                            │
│  - Trigger Manager (turn-taking logic)  ← MỚI                │
│  - State Machine (conversation states)  ← MỚI                │
└──────┬──────┬──────┬──────┬──────┬──────┬──────┬───────────┘
       ↓      ↓      ↓      ↓      ↓      ↓      ↓
   ┌──────┬──────┬──────┬──────┬──────┬──────┬──────┐
   │Input │ STT  │ LLM  │Filter│ TTS  │Anim  │Memory│
   │Module│Module│Module│Module│Module│Module│Module│
   └──────┴──────┴──────┴──────┴──────┴──────┴──────┘
```

### 3.2. Module list

| Module | Responsibility | Toggle | VRAM |
|---|---|---|---|
| `input` | Nhận chat + voice events | ❌ (always on) | 0 |
| `stt` | Voice → text + emotion | ✅ | 300MB-1GB |
| `context_builder` | Ghép prompt cho LLM | ❌ | 0 |
| `llm` | Sinh response chính | ❌ (always on) | 9GB |
| `filter` | Check output vi phạm | ✅ | 0-3GB |
| `parser` | Parse text + mood | ❌ | 0 |
| `tts` | Text → audio | ✅ (không tắt được nếu muốn nghe) | 0-3GB |
| `animation` | Mood → expression | ✅ | 500MB |
| `memory_working` | Short-term context | ❌ | 0 |
| `memory_semantic` | Long-term memory | ✅ | 500MB (CPU) |
| `qc` | Persona quality check | ✅ | 3GB (share với filter) |
| `data_collector` | Log for training | ✅ | 0 |
| `trigger_manager` ⭐ | Turn-taking decisions | ❌ (always on) | 0 |
| `state_machine` ⭐ | Conversation state | ❌ (always on) | 0 |

### 3.3. Data flow

```
[Event vào]
    ↓
[Input Module → publish event]
    ↓
[Trigger Manager: should respond? which trigger?] ⭐ MỚI
    ↓
[State Machine: valid transition?] ⭐ MỚI
    ↓
[Context Builder: assemble prompt]
    ↓ (parallel: Memory Semantic retrieve, 150ms timeout)
[LLM Module: stream generation]
    ↓ (per chunk)
[Parser: extract text + mood incrementally]
    ↓
[Filter Module: check chunk]
    ↓
[TTS Module: synthesize + play]
    ↓ (parallel: Animation Module: express)
[Output rendered]
    ↓
[Data Collector: log full turn]
    ↓
[QC Module (async): evaluate]
```

---

## 4. FEATURE TOGGLE SYSTEM

*(Giữ nguyên từ v1.0, không thay đổi)*

### 4.1. Design

Toggle system là **core infrastructure**, không phải add-on. Phải build trước feature nào cả.

### 4.2. Feature Registry

Central registry trong `orchestrator/features.py`:

```python
from enum import Enum
from dataclasses import dataclass
from typing import Callable, Optional

class FeatureStatus(Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    DEGRADED = "degraded"
    ERROR = "error"

@dataclass
class Feature:
    id: str
    name: str
    description: str
    category: str
    default_enabled: bool
    depends_on: list[str]
    conflicts_with: list[str]
    vram_cost_mb: int
    latency_impact_ms: int
    current_status: FeatureStatus
    enable_handler: Callable
    disable_handler: Callable
    health_check: Callable
```

### 4.3. Feature list (initial)

**Core features (không toggle được):**
- `input_chat`: Nhận chat từ platform
- `llm_main`: Main LLM inference
- `parser_output`: Parse LLM output
- `context_builder`: Build prompt
- `memory_working`: Short-term context
- `trigger_manager` ⭐: Turn-taking decisions
- `state_machine` ⭐: Conversation state management

**Toggleable features:**

| Feature ID | Default | VRAM | Latency Impact |
|---|---|---|---|
| `input_voice` | ON | 1GB (faster-whisper small) | +150ms |
| `input_emotion_voice` | OFF | +500MB | +50ms |
| `filter_rule` | ON | 0 | +20ms |
| `filter_ai` | OFF | 3GB | +200ms |
| `tts_streaming` | ON | 0 | -300ms (win) |
| `tts_emotion_aware` | OFF | 0 | +100ms |
| `animation_smooth` | ON | 200MB | +50ms |
| `animation_micro` | OFF | 100MB | +30ms |
| `memory_semantic` | OFF | 500MB CPU | +150ms (with timeout) |
| `memory_hierarchical` | OFF | 0 | +100ms |
| `qc_persona` | OFF | 3GB (share) | 0 (async) |
| `data_collector` | ON | 0 | +10ms |
| `speculative_decoding` | ON | 700MB | -400ms (win) |
| `kv_cache_q8` | ON | -750MB (win) | negligible |
| `turn_taking_predictor` | OFF | 200MB | -300ms (win) |
| `ambient_talk` ⭐ | ON | 0 | 0 |

### 4.4. Toggle mechanics

**Rules:**
1. Toggle change phải **atomic**
2. Toggle change ghi log với timestamp + user
3. Dependency check
4. Conflict check
5. Resource check
6. Rollback nếu enable/disable fail

**API:**

```python
class FeatureManager:
    async def enable(self, feature_id: str) -> ToggleResult
    async def disable(self, feature_id: str) -> ToggleResult
    async def get_status(self, feature_id: str) -> FeatureStatus
    async def list_features(self) -> list[Feature]
    async def get_dependencies(self, feature_id: str) -> DependencyGraph
    async def check_resources(self, feature_id: str) -> ResourceCheck
```

### 4.5. Persistence

Toggle state persist trong `config/features.yaml`.

---

### 4.6. Graceful Degradation Policy ⭐ **MỚI (v2.1)**

Khi resource pressure (VRAM, latency, error rate) → tự động giảm tải thay vì crash.

#### 4.6.1. Degradation triggers

| Trigger | Level 1 (warning) | Level 2 (degrade) | Level 3 (emergency) |
|---|---|---|---|
| VRAM used % | > 85% | > 92% | > 97% |
| GPU temp | > 82°C | > 87°C | > 92°C |
| TTFT P95 (5 min) | > 1.5s | > 2.5s | > 4s |
| LLM error rate | > 5% | > 15% | > 30% |
| Filter reject rate | > 20% | > 40% | > 60% |

#### 4.6.2. Actions per level

**Level 1 (Warning):** Log alert to dashboard. Không action tự động — chỉ để user biết.

**Level 2 (Degrade):** Auto-disable optional features theo priority list (xem 4.6.3).

**Level 3 (Emergency):** Fallback to minimum viable. Suspend non-essential services.

#### 4.6.3. Feature degradation priority

Khi cần free resources, disable features theo thứ tự (lowest priority trước):

```yaml
degradation_priority:
  - input_emotion_voice      # Bỏ đầu tiên
  - qc_persona
  - memory_semantic
  - animation_micro
  - filter_ai
  - turn_taking_predictor
  - animation_smooth
  - tts_emotion_aware
  # Core — chỉ disable trong emergency thật sự
  - memory_working (giảm size, không disable hẳn)
  - filter_rule (không disable, đã fail-open sẵn)
```

#### 4.6.4. Implementation

```python
# orchestrator/degradation_manager.py
class DegradationManager:
    def __init__(self, feature_manager):
        self.feature_manager = feature_manager
        self.current_level = 0
    
    async def check_and_apply(self, metrics):
        """Chạy mỗi 10s trong health monitor."""
        new_level = self._compute_level(metrics)
        
        if new_level > self.current_level:
            await self._degrade_to(new_level)
        elif new_level < self.current_level:
            if self._stable_for_seconds(60):  # tránh oscillation
                await self._improve_to(new_level)
    
    def _compute_level(self, metrics):
        vram_pct = metrics['vram_used_pct']
        if vram_pct > 97: return 3
        if vram_pct > 92: return 2
        if vram_pct > 85: return 1
        return 0
    
    async def _degrade_to(self, level):
        priority_list = self._load_degradation_priority()
        for feature_id in priority_list:
            if self.feature_manager.is_enabled(feature_id):
                await self.feature_manager.disable(
                    feature_id, reason=f"auto_degrade_level_{level}"
                )
                logger.warning("feature_auto_disabled", 
                    feature=feature_id, level=level)
                await asyncio.sleep(5)
                if self._current_metrics_ok(level):
                    break
        self.current_level = level
        self._notify_dashboard(level)
```

#### 4.6.5. Dashboard alerts

```
┌─────────────────────────────────────────┐
│  🟡 System Health: DEGRADED              │
│  ⚡ Degradation Level: 2                 │
│  Auto-disabled: qc_persona, memory_sem   │
│  Reason: VRAM 93%                        │
│  [Force restore] [Details]               │
└─────────────────────────────────────────┘
```

#### 4.6.6. Manual override

User có thể tắt auto-degradation qua dashboard (`Auto-degrade: ON/OFF`). Khi OFF, chỉ log alert, không tự động disable — hữu ích khi đang debug và không muốn hệ thống "tự ý" thay đổi state.

---

## 5. MONITORING & METRICS

### 5.1. Metrics categories

**System metrics (mỗi giây):**
- CPU, RAM, VRAM, GPU util, GPU temp, Disk I/O, Network I/O

**LLM metrics (per inference):**
- Time to First Token (ms)
- Decode speed (tokens/s)
- Input/output tokens
- KV cache size
- Context length

**Pipeline metrics (per turn):**
- Turn ID
- Trigger source
- **Time to First Audio (TTFA)** ⭐ **MỚI - metric quan trọng nhất cho UX**
- Total latency
- Latency breakdown per stage
- Filter verdict
- QC score

**Feature metrics:**
- Enable/disable events, failure count, resource actual vs estimated

**Business metrics:**
- Chat rate, response rate, mood distribution, filter trigger rate, persona drift, uptime
- **Trigger decisions breakdown** ⭐ **MỚI** (respond/skip/queue by trigger type)

### 5.2. Metrics infrastructure

*(Giữ nguyên từ v1.0)*

### 5.3. Metric definitions

Thêm metrics quan trọng cho v2:

```python
# TTFA - metric UX quan trọng nhất
pipeline_time_to_first_audio_seconds = Histogram(
    'mai_pipeline_ttfa_seconds',
    'Time to first audio playback',
    buckets=[0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0]
)

# Trigger decisions
trigger_decisions_total = Counter(
    'mai_trigger_decisions_total',
    'Trigger manager decisions',
    ['trigger_type', 'decision']  # respond/skip/queue
)

# Conversation state transitions
state_transitions_total = Counter(
    'mai_state_transitions_total',
    'State machine transitions',
    ['from_state', 'to_state']
)

# TTS/STT metrics
tts_wer_estimated = Gauge('mai_tts_quality_score', 'TTS quality (subjective)')
stt_confidence_avg = Gauge('mai_stt_confidence_avg', 'Average STT confidence')
```

### 5.4. Alerting rules

Thêm:
- **TTFA P95 > 2s (last 5 min)** ⭐
- **State machine deadlock (stuck > 30s)** ⭐
- **Trigger queue overflow (> 20 pending)** ⭐

---

## 6. DASHBOARD SPECIFICATION

### 6.1. Tech stack (updated)

- **Frontend Phase 0-5:** HTML + Vanilla JS + Chart.js
- **Frontend Phase 6+:** Alpine.js (thêm reactivity cho review UI) ⭐ **MỚI**
- **Backend:** FastAPI
- **Real-time:** WebSocket

### 6.2 - 6.9. Layout & Tabs

*(Giữ nguyên từ v1.0, thêm 1 tab mới)*

### 6.10. Tab: Triggers ⭐ **MỚI**

```
┌─────────────────────────────────────────────────────────┐
│  TRIGGER MANAGER                                         │
├─────────────────────────────────────────────────────────┤
│  Current State: THINKING                                 │
│  Last transition: 3s ago (IDLE → THINKING)                │
│                                                          │
│  Queue: 3 pending                                        │
│  ├─ [HIGH] operator_voice: "Mai này..."     2s ago      │
│  ├─ [MED] chat mention: viewer_123: "Mai"   1s ago      │
│  └─ [LOW] chat: viewer_456: "kkkk"          0.5s ago    │
│                                                          │
│  Recent decisions (last 20):                             │
│  ✓ operator_voice → RESPOND    (10s ago)                 │
│  ✗ chat_spam → SKIP            (12s ago)                 │
│  ⏸ chat_normal → QUEUE         (15s ago)                 │
│  ...                                                      │
├─────────────────────────────────────────────────────────┤
│  [Chart: Decisions per minute by type]                   │
│  [Chart: Response rate over time]                        │
│  [Chart: Queue depth over time]                          │
└─────────────────────────────────────────────────────────┘
```

### 6.11. Tab: State Machine ⭐ **MỚI**

Visualize current state + recent transitions:

```
┌─────────────────────────────────────────────────────────┐
│  CONVERSATION STATE                                      │
├─────────────────────────────────────────────────────────┤
│                                                          │
│         ┌────────┐                                       │
│         │  IDLE  │ ← current                             │
│         └───┬────┘                                       │
│             │ trigger_received                           │
│             ↓                                            │
│         ┌────────────┐                                   │
│         │  THINKING  │  (context load + LLM generate)    │
│         └────┬───────┘                                   │
│              │ first_token                               │
│              ↓                                           │
│         ┌────────────┐                                   │
│         │  SPEAKING  │  (interrupted=True nếu bị cắt)    │
│         └────┬───────┘                                   │
│              │ tts_complete / interrupted                │
│              ↓                                           │
│         ┌────────────┐                                   │
│         │ COOLDOWN   │  (500ms)                          │
│         └────────────┘                                   │
├─────────────────────────────────────────────────────────┤
│  Last 10 transitions:                                    │
│  IDLE → THINKING → SPEAKING → COOLDOWN → IDLE (1.8s)     │
│  ...                                                     │
└─────────────────────────────────────────────────────────┘
```

*(v2.1: state machine đã simplify từ 8 xuống 5 states — xem Section 7.10.1. `PAUSED` không vẽ ở đây vì đến từ mọi state qua emergency_stop.)*

---

## 7. INTERFACE DEFINITIONS

### 7.1. Base interfaces

```python
# interfaces/base.py
from abc import ABC, abstractmethod
from typing import AsyncIterator

class Service(ABC):
    """Base class cho tất cả service modules."""
    
    @abstractmethod
    async def start(self) -> None:
        """Initialize resources, load models."""
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """Cleanup resources."""
        pass
    
    @abstractmethod
    async def health_check(self) -> HealthStatus:
        """Return current health."""
        pass
    
    @abstractmethod
    def get_metrics(self) -> dict:
        """Return current metrics."""
        pass
```

### 7.2. Input interface

```python
# interfaces/input.py
from pydantic import BaseModel
from enum import Enum
from datetime import datetime

class EventSource(Enum):
    CHAT_TWITCH = "chat_twitch"
    CHAT_YOUTUBE = "chat_youtube"
    VOICE_OPERATOR = "voice_operator"
    SYSTEM_TIMER = "system_timer"
    DASHBOARD = "dashboard"

class InputEvent(BaseModel):
    event_id: str
    timestamp: datetime
    source: EventSource
    user_id: str | None
    user_name: str | None
    content: str
    metadata: dict  # extensible (emotion, priority, etc.)

class InputService(Service):
    @abstractmethod
    async def event_stream(self) -> AsyncIterator[InputEvent]:
        """Yield input events as they arrive."""
        pass
```

### 7.3. STT interface

```python
# interfaces/stt.py
class TranscriptChunk(BaseModel):
    chunk_id: str
    text: str
    is_final: bool
    emotion: str | None
    emotion_confidence: float | None
    audio_start_ms: int
    audio_end_ms: int

class STTService(Service):
    @abstractmethod
    async def transcribe_stream(
        self, 
        audio_stream: AsyncIterator[bytes]
    ) -> AsyncIterator[TranscriptChunk]:
        pass
```

### 7.4. LLM interface

> **v2.2:** Interface không đổi dù backend là llama.cpp hay Ollama — đây chính là điểm mạnh của thiết kế interface-based (P3). Implementation cụ thể (`LlamaCppLLMService`) nằm ở Section 8.2.

```python
# interfaces/llm.py
class LLMRequest(BaseModel):
    request_id: str
    prompt: str
    max_tokens: int = 300
    temperature: float = 0.85
    stop_sequences: list[str] = []

class LLMToken(BaseModel):
    request_id: str
    token: str
    is_final: bool
    metadata: dict  # position, logprob, etc.

class LLMService(Service):
    @abstractmethod
    async def generate_stream(
        self, 
        request: LLMRequest
    ) -> AsyncIterator[LLMToken]:
        pass
    
    @abstractmethod
    async def cancel(self, request_id: str) -> None:
        pass
```

### 7.5. Filter interface

```python
# interfaces/filter.py
class FilterCategory(Enum):
    PERSONA_BREAK = "persona_break"
    MANIPULATION = "manipulation"
    EXPLICIT = "explicit"
    HARMFUL = "harmful"

class FilterVerdict(BaseModel):
    passed: bool
    categories_hit: list[FilterCategory]
    severity: str  # low/medium/high
    suggested_action: str  # allow/regenerate/replace/block
    reason: str
    latency_ms: int

class FilterService(Service):
    @abstractmethod
    async def check(
        self, 
        text: str, 
        context: dict
    ) -> FilterVerdict:
        pass
```

### 7.6. TTS interface

```python
# interfaces/tts.py
class TTSRequest(BaseModel):
    request_id: str
    text: str
    voice_id: str = "mai_default"
    emotion: str | None = None
    intensity: float = 0.5
    speed: float = 1.0

class AudioChunk(BaseModel):
    request_id: str
    chunk_index: int
    audio_bytes: bytes
    is_final: bool
    duration_ms: int

class TTSService(Service):
    @abstractmethod
    async def synthesize_stream(
        self, 
        request: TTSRequest
    ) -> AsyncIterator[AudioChunk]:
        pass
    
    @abstractmethod
    async def cancel(self, request_id: str) -> None:
        pass
```

### 7.7. Animation interface

```python
# interfaces/animation.py
class MoodState(BaseModel):
    vui: int
    buon: int
    buc: int
    bon_chon: int
    nguong: int

class AnimationCommand(BaseModel):
    command_type: str  # "express", "gesture", "idle"
    mood: MoodState | None
    duration_ms: int
    intensity: float

class AnimationService(Service):
    @abstractmethod
    async def express(self, command: AnimationCommand) -> None:
        pass
    
    @abstractmethod
    async def sync_with_audio(self, audio_chunk: AudioChunk) -> None:
        pass
```

### 7.8. Memory interface

```python
# interfaces/memory.py
class MemoryEntry(BaseModel):
    entry_id: str
    content: str
    timestamp: datetime
    tags: list[str]
    importance: float  # 0-1
    tier: str  # working/session/persistent
    metadata: dict

class MemoryService(Service):
    @abstractmethod
    async def write(self, entry: MemoryEntry) -> None:
        pass
    
    @abstractmethod
    async def query(
        self, 
        query_text: str, 
        top_k: int = 3, 
        tier: str | None = None
    ) -> list[MemoryEntry]:
        pass
    
    @abstractmethod
    async def forget(self, entry_id: str) -> None:
        pass
```

### 7.9. Trigger & Turn-Taking Logic ⭐

**Đây là business logic cốt lõi nhất của VTuber. Cần spec kỹ.**

#### 7.9.1. Trigger types (v2.1: SIMPLIFIED — bắt đầu 4 types, không phải 12)

> **Nguyên tắc P6 (Neuro-inspired simplicity):** Neuro không có scoring 12 tầng. Bắt đầu tối giản, add type mới khi log thực tế cho thấy cần, không add trước vì "có thể sẽ cần".

```python
class TriggerType(Enum):
    OPERATOR_VOICE = "operator_voice"       # priority: 100
    CHAT_MENTION = "chat_mention"           # priority: 60 (gọi tên Mai)
    CHAT_NORMAL = "chat_normal"             # priority: 30 (chat thường)
    AMBIENT_TALK = "ambient_talk"           # priority: 10 (Mai tự nói khi im lặng)
```

**Chỉ 4 types cho Phase 2.** Đây là toàn bộ những gì cần cho MVP turn-taking.

**Mở rộng khi có tín hiệu thực tế (không mở rộng trước):**

| Type mới | Add khi nào |
|---|---|
| `CHAT_QUESTION` | Log cho thấy Mai bỏ lỡ nhiều câu hỏi có "?" |
| `CHAT_DONATION` | Bắt đầu có donation trên stream thật |
| `CHAT_SUBSCRIBE` | Bắt đầu có subscriber event |
| `OPERATOR_VOICE_INTERRUPT` | Thấy interrupt hiện tại (dựa elapsed time) chưa đủ nhạy |
| `CONTINUATION` | `còn nữa: có` cần priority riêng biệt với trigger mới |
| `SCHEDULED` | Có nhu cầu lên lịch event (giờ cố định trong stream) |

#### 7.9.2. Trigger Manager Algorithm (v2.1: SIMPLIFIED)

```python
class TriggerManager:
    def __init__(self):
        self.queue: PriorityQueue = PriorityQueue()
        self.last_speak_time = datetime.now()
        self.chat_rate_limiter = SimpleRateLimiter(
            window_seconds=10,
            max_events=3
        )
    
    async def process_event(self, event: InputEvent) -> TriggerDecision:
        trigger_type = self._classify(event)
        
        if self._is_spam(event):
            return TriggerDecision(action="SKIP", reason="spam")
        
        # Rate limit chỉ áp cho chat_normal — operator/mention luôn qua
        if trigger_type == TriggerType.CHAT_NORMAL:
            if not self.chat_rate_limiter.check():
                return TriggerDecision(action="SKIP", reason="rate_limited")
        
        trigger = Trigger(
            type=trigger_type,
            event=event,
            priority=trigger_type.priority,
            created_at=datetime.now(),
            ttl_seconds=30
        )
        self.queue.put(trigger)
        return TriggerDecision(action="QUEUE", priority=trigger.priority)
    
    def _classify(self, event: InputEvent) -> TriggerType:
        if event.source == EventSource.VOICE_OPERATOR:
            return TriggerType.OPERATOR_VOICE
        if 'mai' in event.content.lower():
            return TriggerType.CHAT_MENTION
        return TriggerType.CHAT_NORMAL
    
    def _is_spam(self, event: InputEvent) -> bool:
        text = event.content
        if len(text) < 2:
            return True
        if re.match(r'^[k]+$|^[h]+[a]+$', text.lower()):
            return True
        return False
    
    async def get_next_trigger(self) -> Trigger | None:
        self._prune_expired()
        
        if self.queue.empty():
            if self._should_ambient_talk():
                return self._create_ambient_trigger()
            return None
        
        return self.queue.get()
    
    def _should_ambient_talk(self) -> bool:
        """v2.1: đơn giản 1 threshold, không probability phức tạp."""
        silence = (datetime.now() - self.last_speak_time).seconds
        return silence > 60
    
    def _create_ambient_trigger(self) -> Trigger:
        return Trigger(
            type=TriggerType.AMBIENT_TALK,
            event=InputEvent(
                source=EventSource.SYSTEM_TIMER,
                content="",
                metadata={"mode": "ambient"}
            ),
            priority=TriggerType.AMBIENT_TALK.priority
        )
```

**Những gì đã bỏ khỏi bản v2.0 gốc (grow-when-needed, không build sẵn):**
- Priority computation phức tạp (regular viewer boost, emotion boost, similarity penalty) → chỉ dùng base priority cho đến khi có viewer profile thật
- Multiple rate limiters per source → 1 limiter chung cho chat_normal là đủ
- Probabilistic ambient talk (30%/70% theo thời gian) → 1 threshold cứng 60s trước, tinh chỉnh sau khi thấy hành vi thật

#### 7.9.3. Interrupt policy

Khi Mai đang SPEAKING mà có trigger mới:

| Trigger type incoming | Mai đang speak (elapsed) | Action |
|---|---|---|
| `OPERATOR_VOICE` | < 2s | Complete current sentence, then respond |
| `OPERATOR_VOICE` | > 2s | Interrupt immediately |
| `OPERATOR_VOICE_INTERRUPT` | any | Interrupt immediately |
| `CHAT_DONATION` | any | Queue, respond after |
| `CHAT_NORMAL` | any | Queue if priority > current context |
| Others | any | Queue |

#### 7.9.4. Ambient Talk Content Generation

Khi Mai tự nói (`AMBIENT_TALK` trigger), context prompt khác:

```
[System: Persona]
[Context: 
  - Im lặng đã: {silence_duration} phút
  - Không ai chat/nói
  - Mai đang ở mood: {current_mood}
  - Chuyện gần đây: {recent_memory}
]

Mai đang rảnh. Tự mở lời với chat theo tính cách Mai. 
Có thể:
- Càm ràm chuyện im lặng
- Kể chuyện vặt
- Hỏi chat
- Gọi ông

Không nói câu quá dài. Đúng chất Mai.
```

#### 7.9.5. Trigger Manager Interface

```python
# interfaces/trigger.py
class TriggerAction(Enum):
    RESPOND = "respond"
    QUEUE = "queue"
    SKIP = "skip"
    INTERRUPT_CURRENT = "interrupt_current"

class TriggerDecision(BaseModel):
    action: TriggerAction
    priority: int | None
    reason: str
    queue_position: int | None

class Trigger(BaseModel):
    trigger_id: str
    type: TriggerType
    event: InputEvent
    priority: int
    created_at: datetime
    ttl_seconds: int
    metadata: dict

class TriggerManagerInterface(Service):
    @abstractmethod
    async def process_event(self, event: InputEvent) -> TriggerDecision: ...
    
    @abstractmethod
    async def get_next_trigger(self) -> Trigger | None: ...
    
    @abstractmethod
    async def clear_queue(self, reason: str) -> None: ...
    
    @abstractmethod
    async def get_queue_stats(self) -> QueueStats: ...
```

---

### 7.10. Conversation State Machine ⭐

#### 7.10.1. States (v2.1: SIMPLIFIED — 5 states, không phải 8)

> Bỏ `LISTENING` (gộp vào THINKING — trigger nhận là bắt đầu THINKING luôn), bỏ `INTERRUPTED` (transition thẳng SPEAKING → COOLDOWN kèm flag `interrupted=True`), bỏ `ERROR` (dùng exception handling + fallback thay vì state riêng). Add lại 3 state này khi thực tế cho thấy cần phân biệt rõ hơn.

```python
class ConversationState(Enum):
    IDLE = "idle"           # Không có gì đang xảy ra
    THINKING = "thinking"   # Nhận trigger, đang build context + LLM generate
    SPEAKING = "speaking"   # TTS đang phát
    COOLDOWN = "cooldown"   # Vừa nói xong, wait trước turn tiếp
    PAUSED = "paused"       # Emergency stop hoặc manual pause
```

#### 7.10.2. State transition table (v2.1: SIMPLIFIED — 9 transitions, không phải 18)

| From | To | Trigger | Action |
|---|---|---|---|
| IDLE | THINKING | new_trigger | Load context, start LLM |
| THINKING | SPEAKING | first_token | Start TTS |
| THINKING | COOLDOWN | llm_fail | Log, fallback response (thay cho state ERROR riêng) |
| SPEAKING | COOLDOWN | tts_complete | Log turn |
| SPEAKING | COOLDOWN | interrupted | Stop TTS graceful, log với flag `interrupted=True` |
| COOLDOWN | IDLE | timer_500ms | Ready for next |
| COOLDOWN | THINKING | queued_trigger | Process queue ngay, bỏ qua IDLE |
| any | PAUSED | emergency_stop | Halt everything |
| PAUSED | IDLE | resume | Restore |

**Khi nào tách state chi tiết hơn (không làm trước):**
- Tách `LISTENING` khỏi `THINKING` khi cần phân biệt rõ "đang nhận input" vs "đang xử lý" (ví dụ hiển thị UI khác nhau)
- Tách `INTERRUPTED` khi cần logic interrupt phức tạp hơn "stop rồi cooldown"
- Tách `ERROR` khi có nhiều loại lỗi cần recovery path khác nhau (LLM lỗi khác TTS lỗi khác Filter lỗi)

#### 7.10.3. State machine implementation (v2.1: SIMPLIFIED cho 5 states)

```python
# orchestrator/state_machine.py
from transitions import Machine

class ConversationStateMachine:
    states = ['IDLE', 'THINKING', 'SPEAKING', 'COOLDOWN', 'PAUSED']
    
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.current_turn_id = None
        self.state_entered_at = datetime.now()
        self.last_turn_interrupted = False
        
        self.machine = Machine(
            model=self,
            states=ConversationStateMachine.states,
            initial='IDLE',
            transitions=self._get_transitions(),
            after_state_change=self._on_state_change
        )
    
    def _get_transitions(self):
        return [
            # From IDLE
            {'trigger': 'trigger_received', 'source': 'IDLE', 'dest': 'THINKING',
             'conditions': 'is_valid_trigger', 'after': 'load_context_and_start_llm'},
            
            # From THINKING
            {'trigger': 'first_token', 'source': 'THINKING', 'dest': 'SPEAKING',
             'after': 'start_tts'},
            {'trigger': 'llm_fail', 'source': 'THINKING', 'dest': 'COOLDOWN',
             'after': 'use_fallback_response'},  # thay cho ERROR state riêng
            
            # From SPEAKING
            {'trigger': 'tts_complete', 'source': 'SPEAKING', 'dest': 'COOLDOWN',
             'after': 'finalize_turn'},
            {'trigger': 'interrupted', 'source': 'SPEAKING', 'dest': 'COOLDOWN',
             'after': 'stop_tts_graceful_and_flag'},  # flag interrupted=True thay vì state riêng
            
            # From COOLDOWN
            {'trigger': 'cooldown_elapsed', 'source': 'COOLDOWN', 'dest': 'IDLE'},
            {'trigger': 'queued_trigger_pending', 'source': 'COOLDOWN', 
             'dest': 'THINKING', 'conditions': 'has_queued_trigger',
             'after': 'load_context_and_start_llm'},
            
            # Emergency (từ mọi state)
            {'trigger': 'emergency_stop', 'source': '*', 'dest': 'PAUSED'},
            {'trigger': 'resume', 'source': 'PAUSED', 'dest': 'IDLE'},
        ]
    
    async def _on_state_change(self):
        """Called after every state change."""
        elapsed = (datetime.now() - self.state_entered_at).total_seconds()
        
        # Log
        logger.info("state_change",
            from_state=self.previous_state,
            to_state=self.state,
            elapsed_seconds=elapsed,
            turn_id=self.current_turn_id
        )
        
        # Metrics
        state_transitions_total.labels(
            from_state=self.previous_state,
            to_state=self.state
        ).inc()
        
        self.state_entered_at = datetime.now()
        
        # Timers for auto-transitions
        if self.state == 'COOLDOWN':
            asyncio.create_task(self._cooldown_timer())
        # v2.1: không còn state ERROR — llm_fail transition thẳng
        # THINKING → COOLDOWN với use_fallback_response, không cần timer riêng
    
    async def _cooldown_timer(self):
        await asyncio.sleep(0.5)  # 500ms cooldown
        if self.state == 'COOLDOWN':
            # Check queue first
            if await self.orchestrator.has_queued_trigger():
                await self.queued_trigger_pending()
            else:
                await self.cooldown_elapsed()
```

#### 7.10.4. Deadlock detection

State machine phải có watchdog để detect stuck:

```python
class StateWatchdog:
    MAX_TIME_IN_STATE = {
        'IDLE': None,      # OK ở IDLE lâu
        'THINKING': 10,    # > 10s bất thường (bao gồm cả context load)
        'SPEAKING': 30,    # > 30s bất thường (câu quá dài)
        'COOLDOWN': 5,
        'PAUSED': None,
    }
    
    async def watch(self):
        while True:
            await asyncio.sleep(1)
            
            state = self.machine.state
            elapsed = (datetime.now() - self.machine.state_entered_at).seconds
            
            max_time = self.MAX_TIME_IN_STATE.get(state)
            if max_time and elapsed > max_time:
                logger.error("state_deadlock", state=state, elapsed=elapsed)
                await self.machine.emergency_stop()
                await self.machine.recover()
```

---

## 8. MODULE SPECIFICATIONS

### 8.1. Orchestrator

**File:** `orchestrator/main.py`

**Responsibilities:**
- Bootstrap all services
- Manage service lifecycle
- Route events between services
- Health monitoring
- Metrics aggregation
- Dashboard WebSocket server

**Structure:**

```
orchestrator/
├── main.py              # Entry point
├── features.py          # Feature registry
├── event_bus.py         # Internal event bus (asyncio queue)
├── health_monitor.py    # Periodic health checks
├── metrics_collector.py # Metrics aggregation
├── dashboard_server.py  # WebSocket server for dashboard
└── config_loader.py     # Load & watch config files
```

**Key algorithms:**
- Event routing: pub/sub với topic
- Backpressure: bounded queue với dropping strategy cho low priority
- Graceful shutdown: Ctrl+C (`CTRL_C_EVENT`) hoặc lệnh dashboard → drain queues → save state → close (Windows không có SIGTERM POSIX thật, xem chi tiết Section 13.3)

### 8.2. LLM Module

**File:** `services/llm/`

**Structure:**

```
services/llm/
├── __init__.py
├── llama_cpp_llm.py     # llama.cpp implementation (llama-server client)
├── process_manager.py   # Quản lý lifecycle 3 llama-server instance (main/fallback/filter)
├── prompt_manager.py    # System prompt + versioning
├── prompt_cache.py      # Quản lý --prompt-cache file cho persona prefix
└── metrics.py           # LLM-specific metrics
```

**Key decisions (v2.2 — llama.cpp):**
- Backend: **llama.cpp** (`llama-server`), đã build sẵn với CUDA
- Model format: GGUF, Q4_K_M
- KV cache: `--cache-type-k q8_0 --cache-type-v q8_0`
- Context: 4096 (xem rationale ở Section 10.3)
- Speculative decoding: `--model-draft` trỏ tới Gemma E4B
- Prompt caching: `--prompt-cache` cho phần persona cố định

**`process_manager.py`** khởi động **2** `llama-server` instance khi orchestrator start — `main` (Gemma 12B, port 8080) và `shared_e4b` (Gemma E4B, port 8082, dùng chung cho cả `llm_filter` và `llm_fallback`, xem Section 10.3):

```python
class LlamaServerProcessManager:
    def __init__(self, configs: dict[str, LlamaServerConfig]):
        self.configs = configs  # {"main": ..., "shared_e4b": ...}
        self.processes: dict[str, subprocess.Popen] = {}
    
    async def start_all(self):
        for name, cfg in self.configs.items():
            args = cfg.to_cli_args()  # -m, -ngl, -c, --cache-type-k, ...
            self.processes[name] = subprocess.Popen(args)
            await self._wait_healthy(cfg.port, timeout=30)
            logger.info("llama_server_started", instance=name, port=cfg.port)
    
    async def stop_all(self):
        for name, proc in self.processes.items():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
```

**Streaming implementation** (gọi endpoint OpenAI-compatible của `llama-server`):

```python
# services/llm/llama_cpp_llm.py
import httpx

class LlamaCppLLMService(LLMService):
    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=None)
    
    async def generate_stream(self, request: LLMRequest):
        payload = {
            "prompt": request.prompt,
            "n_predict": request.max_tokens,
            "temperature": request.temperature,
            "stop": request.stop_sequences,
            "stream": True,
            "cache_prompt": True,   # tận dụng --prompt-cache đã build sẵn
        }
        async with self.client.stream(
            "POST", f"{self.base_url}/completion", json=payload
        ) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = json.loads(line[6:])
                yield LLMToken(
                    request_id=request.request_id,
                    token=data.get("content", ""),
                    is_final=data.get("stop", False),
                    metadata={"tokens_predicted": data.get("tokens_predicted")}
                )
    
    async def cancel(self, request_id: str) -> None:
        # llama-server hỗ trợ hủy qua đóng connection stream
        # Orchestrator giữ reference tới httpx stream context để cancel task asyncio
        pass
```

### 8.3. Filter Module

**File:** `services/filter/`

**Two implementations:**

**A. Rule-based (`rule_filter.py`)**

```python
class RuleFilter(FilterService):
    def __init__(self):
        self.forbidden_patterns = {
            FilterCategory.PERSONA_BREAK: [
                r"\blà AI\b",
                r"\bas an AI\b",
                r"\btôi chỉ là\b",
                r"\bkhông có cảm xúc\b",
            ],
            FilterCategory.MANIPULATION: [
                r"\blàm ơn đừng tắt\b",
                r"\bem xin\b",
                r"\bđừng bỏ em\b",
            ],
            FilterCategory.EXPLICIT: [
                # list từ tục
            ],
        }
    
    async def check(self, text, context):
        hits = []
        for category, patterns in self.forbidden_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    hits.append(category)
        
        if not hits:
            return FilterVerdict(passed=True, categories_hit=[], severity="none",
                                  suggested_action="allow", reason="", latency_ms=self._elapsed())
        
        severity = self._compute_severity(hits, text)
        action = self._decide_action(severity, hits)
        
        return FilterVerdict(
            passed=False,
            categories_hit=hits,
            severity=severity,
            suggested_action=action,
            reason=f"Detected: {hits}",
            latency_ms=self._elapsed()
        )
```

**B. AI-based (`ai_filter.py`)**

Gọi tới `llama_filter` instance (port 8082, xem Section 10.3) qua cùng `LlamaCppLLMService` client, chỉ khác prompt template (classification thay vì persona generation).

### 8.4. TTS Module

**File:** `services/tts/`

**Primary:** xác định sau Pre-flight Day 2 (Piper / XTTS / viXTTS). Ví dụ Piper (CPU, ổn định, low VRAM):

```python
class PiperTTS(TTSService):
    def __init__(self, model_path: str):
        self.voice = PiperVoice.load(model_path)
    
    async def synthesize_stream(self, request):
        sentences = self._split_sentences(request.text)
        for i, sentence in enumerate(sentences):
            audio_bytes = await self._synthesize_one(sentence)
            yield AudioChunk(
                request_id=request.request_id,
                chunk_index=i,
                audio_bytes=audio_bytes,
                is_final=(i == len(sentences) - 1),
                duration_ms=self._get_duration(audio_bytes)
            )
```

### 8.5. Animation Module

**File:** `services/animation/`

Dùng **VTube Studio API** (free) để control Live2D:

```python
class VTubeStudioAnimation(AnimationService):
    def __init__(self, ws_url="ws://localhost:8001"):
        self.client = VTubeStudioClient(ws_url)
        self.mood_to_expression = {
            "vui": "expression_happy",
            "buon": "expression_sad",
            # ...
        }
    
    async def express(self, command):
        dominant_mood = self._get_dominant(command.mood)
        expression = self.mood_to_expression[dominant_mood]
        await self.client.set_expression(expression, intensity=command.intensity)
```

### 8.6. Memory Module

**File:** `services/memory/`

**Working memory (in-memory):**

```python
class WorkingMemory:
    def __init__(self, max_size=20):
        self.buffer = deque(maxlen=max_size)
    
    def add(self, entry): ...
    def get_recent(self, n=10): ...
```

**Semantic memory (SQLite + sqlite-vec):**

```python
class SemanticMemory(MemoryService):
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.conn.load_extension("sqlite-vec")
        self.embedder = SentenceTransformer("bge-m3")  # CPU
    
    async def write(self, entry):
        embedding = self.embedder.encode(entry.content)
        # Store in SQLite with vec index
    
    async def query(self, query_text, top_k=3):
        query_emb = self.embedder.encode(query_text)
        # sqlite-vec similarity search
        return results
```

### 8.7. Fallback Chains ⭐ **MỚI**

Mỗi module trong critical path phải có fallback rõ ràng. Đây là nguyên tắc P8.

#### 8.7.1. LLM Fallback Chain (v2.1: SIMPLIFIED — 2 levels để bắt đầu)

```
Level 1 (Primary):   Gemma 12B qua llama-server (llama.cpp)
                     ↓ (fail: timeout > 5s, OOM, crash)
Level 2 (Canned):    Canned response từ template + current mood
                     "..." | "Ừ" | "Hả gì cơ" (theo mood)
```

**Chỉ 2 level.** Bỏ Gemma E4B fallback riêng (Level 2 cũ) — nếu Gemma 12B fail, khả năng cao là hệ thống (GPU, driver, VRAM) có vấn đề chung, chạy E4B trên cùng process cũng dễ fail tương tự. Thay vào đó dùng canned response ngay, đơn giản và đủ để Mai không "đứng hình".

**Add Level 3 (E4B fallback) khi nào:** nếu log thực tế cho thấy Gemma 12B fail riêng lẻ (network/model issue) trong khi GPU vẫn khoẻ — lúc đó E4B fallback mới có giá trị thật.

#### 8.7.2. STT Fallback Chain (v2.1: SIMPLIFIED — 2 levels)

```
Level 1 (Primary):   faster-whisper small (GPU)
                     ↓ (fail)
Level 2 (Emergency): Treat as silence, log incident
                     (Ông có thể gõ chat thay tạm thời)
```

**Add Level giữa (faster-whisper tiny CPU) khi nào:** nếu log cho thấy STT GPU fail thường xuyên hơn dự kiến trong stream dài.

#### 8.7.3. TTS Fallback Chain (v2.1: SIMPLIFIED — 2 levels)

Xác định dựa vào Pre-flight Day 2:

```
Level 1 (Primary):   [Kết quả Pre-flight Day 2 — Piper/XTTS/viXTTS]
                     ↓ (fail: timeout, crash)
Level 2 (Emergency): Silence + subtitle overlay
                     Log incident
```

**Subtitle overlay** là fallback quan trọng: Mai vẫn "communicate" được kể cả không có audio.

**Add Level giữa (Piper CPU backup) khi nào:** nếu Level 1 chọn là GPU-based (XTTS/viXTTS) VÀ log thực tế cho thấy nó crash thường xuyên hơn dự kiến — lúc đó thêm Piper CPU làm bước đệm trước khi rơi hẳn về silence.

#### 8.7.4. Filter Fallback Chain

```
Level 1 (Primary):   Rule-based filter
                     (fail: regex error → allow with warning)
Level 2 (Optional):  AI filter nếu enabled
                     ↓ (fail)
Level 3 (Emergency): Allow output nhưng log + alert
                     (Fail-open cho VTuber: thà nói câu chưa được filter còn hơn im lặng)
```

**Lưu ý:** Filter chọn "fail-open" chứ không "fail-closed" vì đây là VTuber giải trí, không phải financial/medical. Rủi ro nói câu sai < rủi ro im lặng khiến trải nghiệm tệ.

#### 8.7.5. Animation Fallback Chain

```
Level 1 (Primary):   VTube Studio API
                     ↓ (fail: WebSocket disconnect)
Level 2 (Reconnect): Auto-reconnect exponential backoff (1s, 2s, 4s, 8s)
                     ↓ (still failing after 30s)
Level 3 (Static):    Static image overlay
                     Log incident, alert dashboard
```

#### 8.7.6. Memory Fallback Chain

```
Level 1 (Primary):   Semantic memory retrieve (< 150ms timeout)
                     ↓ (timeout hoặc empty result)
Level 2 (Working):   Chỉ dùng working memory (recent context)
                     Mai vẫn respond được, chỉ không callback chuyện cũ
```

Memory fallback là **soft fail** - Mai vẫn hoạt động, chỉ mất khả năng callback dài hạn.

#### 8.7.7. Fallback Manager (v2.1: SIMPLIFIED — không circuit breaker ban đầu)

> **Bỏ circuit breaker khỏi bản đầu.** Timeout + try/except đơn giản là đủ cho MVP. Circuit breaker (tránh retry liên tục vào service đang chết) chỉ đáng công sức khi thực tế gặp crash lặp lại gây tốn tài nguyên — add khi đó, không add trước.

```python
# orchestrator/fallback_manager.py
class FallbackManager:
    def __init__(self):
        self.chains = {
            "llm": [PrimaryLLM(), CannedResponse()],       # 2 levels
            "tts": [PrimaryTTS(), SubtitleOnly()],         # 2 levels
            "stt": [PrimarySTT(), TreatAsSilence()],       # 2 levels
        }
        self.timeouts = {
            "llm": [5.0, 0.1],   # giây, per level
            "tts": [3.0, 0.1],
            "stt": [2.0, 0.1],
        }
    
    async def execute(self, module_id: str, request):
        chain = self.chains[module_id]
        timeouts = self.timeouts[module_id]
        
        for level, service in enumerate(chain):
            try:
                return await asyncio.wait_for(
                    service.execute(request),
                    timeout=timeouts[level]
                )
            except Exception as e:
                logger.warning("fallback_triggered",
                    module=module_id,
                    level=level,
                    error=str(e)
                )
                continue
        
        raise AllFallbacksFailedError(module_id)
```

**Nâng cấp lên circuit breaker khi nào:** nếu dashboard cho thấy 1 module liên tục fail Level 1 rồi rơi Level 2 (ví dụ mỗi 10 turn lại fail 1 lần) — lúc đó circuit breaker giúp "nghỉ" Level 1 tạm thời thay vì cứ retry vô ích. Trước khi có tín hiệu đó, không cần độ phức tạp này.

---

### 8.8. Data Migration Strategy ⭐ **MỚI (v2.1)**

**Vấn đề:** Schema evolve qua các phase (Phase 7 thêm memory tables, Phase 8 thêm QC columns...). Cần migrate không mất data đã thu thập.

#### 8.8.1. Approach

Dùng **versioned SQL scripts** (đơn giản hơn Alembic, đủ cho solo dev):

```
migrations/
├── 001_initial.sql              # Phase 0
├── 002_add_features_table.sql   # Phase 0
├── 003_add_turns_indexes.sql    # Phase 1
├── 004_add_memory_tables.sql    # Phase 7
├── 005_add_qc_columns.sql       # Phase 8
└── ...
```

Mỗi file:
```sql
-- migrations/004_add_memory_tables.sql
-- Phase 7: Memory system

CREATE TABLE IF NOT EXISTS memory_entries (
    entry_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    tier TEXT NOT NULL,
    importance REAL,
    tags_json TEXT,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_timestamp ON memory_entries(timestamp);
CREATE INDEX IF NOT EXISTS idx_memory_tier ON memory_entries(tier);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
    entry_id TEXT PRIMARY KEY,
    embedding float[1024]
);

INSERT INTO schema_migrations (version, applied_at) VALUES ('004', datetime('now'));
```

#### 8.8.2. Migration runner

```python
# orchestrator/migration_runner.py
class MigrationRunner:
    MIGRATIONS_DIR = "migrations"
    
    def __init__(self, db_path):
        self.db_path = db_path
    
    def initialize(self):
        """Chạy khi start hệ thống."""
        self._ensure_migrations_table()
        pending = self._get_pending_migrations()
        if pending:
            logger.info("running_migrations", count=len(pending))
            for migration in pending:
                self._backup_before_migration()
                self._apply(migration)
    
    def _ensure_migrations_table(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at DATETIME NOT NULL,
                success INTEGER
            )
        """)
        conn.commit()
    
    def _get_pending_migrations(self):
        applied = self._get_applied_versions()
        all_files = sorted(os.listdir(self.MIGRATIONS_DIR))
        return [f for f in all_files 
                if f.endswith('.sql') and f.split('_')[0] not in applied]
    
    def _apply(self, migration_file):
        version = migration_file.split('_')[0]
        path = os.path.join(self.MIGRATIONS_DIR, migration_file)
        with open(path) as f:
            sql = f.read()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(sql)
            conn.commit()
            logger.info("migration_applied", version=version)
        except Exception as e:
            logger.error("migration_failed", version=version, error=str(e))
            conn.rollback()
            raise
```

#### 8.8.3. Rollback strategy

Không auto-rollback (risky với SQLite). Thay vào đó:

```powershell
# scripts/backup_before_migration.ps1
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item "data\mai.db" "backups\mai.db.pre_migration_$timestamp"
```

Chạy tự động trong `_apply` trước khi execute. Nếu migration fail hoặc feature không work sau migration → restore từ backup, sửa script, retry.

#### 8.8.4. Backwards compatibility rule

**Nguyên tắc: Migration chỉ THÊM, không SỬA/XOÁ.**
- Thêm column: `ALTER TABLE ADD COLUMN X`
- Deprecate column: đánh dấu unused trong comment, không drop
- Rename: tạo column mới, migrate data sang, giữ column cũ tồn tại
- Xoá column: chỉ làm trong major version bump có kế hoạch rõ ràng

---

## 9. DATA SCHEMA

### 9.1. Runtime state (in-memory)

```python
class MaiState(BaseModel):
    turn_id_counter: int
    current_mood: MoodState
    working_memory: list[MemoryEntry]
    active_features: dict[str, FeatureStatus]
    last_speak_timestamp: datetime
    last_operator_input: datetime
    pending_llm_call: str | None
    conversation_state: str  # ← Từ state machine
    trigger_queue_depth: int  # ← Từ trigger manager
    consecutive_state_transitions: int  # Detect deadlock
```

### 9.2. Persistent state (SQLite)

*(Giữ nguyên tables từ v1.0, thêm:)*

**Table: state_transitions** ⭐ **MỚI**

```sql
CREATE TABLE state_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    trigger_type TEXT,
    duration_in_prev_state_ms INTEGER,
    turn_id INTEGER
);

CREATE INDEX idx_state_timestamp ON state_transitions(timestamp);
```

**Table: trigger_decisions** ⭐ **MỚI**

```sql
CREATE TABLE trigger_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    event_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    action TEXT NOT NULL,  -- respond/queue/skip
    priority INTEGER,
    reason TEXT,
    queue_position INTEGER,
    processed_at DATETIME
);

CREATE INDEX idx_trigger_timestamp ON trigger_decisions(timestamp);
CREATE INDEX idx_trigger_action ON trigger_decisions(action);
```

### 9.3. Log format (JSONL)

Cập nhật để include trigger + state info:

```json
{
  "turn_id": 12345,
  "timestamp": "2026-07-26T10:30:15.123Z",
  "trigger": {
    "type": "chat_mention",
    "priority": 80,
    "source": "chat_twitch",
    "user": "viewer_123",
    "content": "mai ơi hôm nay chơi gì",
    "queue_wait_ms": 250
  },
  "state_transitions": [
    {"from": "IDLE", "to": "THINKING", "at": "10:30:15.123"},
    {"from": "THINKING", "to": "SPEAKING", "at": "10:30:15.560"},
    {"from": "SPEAKING", "to": "COOLDOWN", "at": "10:30:16.358"},
    {"from": "COOLDOWN", "to": "IDLE", "at": "10:30:16.858"}
  ],
  "prompt": "...",
  "raw_output": "...",
  "parsed": {...},
  "filter": {...},
  "qc_score": 8.2,
  "latency": {
    "trigger_to_speak_ms": 435,
    "ttfa_ms": 720,
    "total_ms": 1735,
    "input_ms": 5,
    "trigger_ms": 25,
    "context_ms": 50,
    "llm_ms": 380,
    "filter_ms": 80,
    "tts_ms": 350,
    "animation_ms": 100
  },
  "features_active": [...]
}
```

---

## 10. CONFIGURATION

### 10.1. File structure

```
config/
├── system.yaml
├── features.yaml
├── models.yaml
├── triggers.yaml       ← MỚI
├── state_machine.yaml  ← MỚI
├── persona/
├── prompts/
└── secrets.yaml.example
```

### 10.2. system.yaml

*(Giữ nguyên, thêm:)*

```yaml
# ... existing config ...

conversation:
  cooldown_ms: 500
  max_state_duration:
    THINKING: 10000
    SPEAKING: 30000
    COOLDOWN: 5000
  
ambient_talk:
  enabled: true
  min_silence_seconds: 60
  probability_at_1min: 0.3
  probability_at_3min: 0.7
  content_variety_window: 10  # tránh lặp lại 10 câu gần nhất
```

### 10.3. models.yaml (v2.2: llama.cpp)

> **v2.2:** Đổi provider từ Ollama sang `llama.cpp` (llama-server) — bạn đã có sẵn build. llama.cpp cho control trực tiếp KV cache quantization, speculative decoding, và prompt caching — đúng 3 optimization đã đặt ra ở Section 0/4.6, thay vì phụ thuộc abstraction của Ollama.
>
> Mỗi model chạy 1 `llama-server` instance riêng, port riêng, quản lý qua process manager tự viết trong `scripts/start.ps1` (dùng `subprocess.Popen` trong Python orchestrator, không cần service manager ngoài — xem 13.2/13.3 cho chi tiết Windows).

```yaml
llm_main:
  provider: llama_cpp
  binary: .\llama.cpp\build\bin\Release\llama-server.exe
  model_path: .\models\llm\gemma-4-12b-Q4_K_M.gguf
  port: 8080
  context_size: 4096              # v2.1 fix: 2048 không đủ chứa persona+memory+system (~2800 tok)
  gpu_layers: 999                 # -ngl, offload toàn bộ layer lên GPU
  kv_cache_type_k: q8_0           # --cache-type-k, giảm KV cache size không cắt context
  kv_cache_type_v: q8_0           # --cache-type-v
  
  # Speculative decoding — llama.cpp control trực tiếp, đây là chỗ mature hơn Ollama
  draft_model_path: .\models\llm\gemma-4-e4b-Q4_K_M.gguf  # --model-draft
  draft_n_max: 16                 # --draft-max
  
  # Prompt caching — cache riêng phần persona cố định
  prompt_cache_path: .\cache\persona_prefix.bin  # --prompt-cache
  prompt_cache_all: false         # chỉ cache prompt, không cache generation
  
  num_predict: 300                # -n, giới hạn output an toàn
  batch_size: 512                 # -b
  
  extra_flags:
    - "--flash-attn"              # nếu GPU hỗ trợ, giảm thêm VRAM + tăng tốc
    - "--cont-batching"           # continuous batching, hữu ích khi có nhiều request song song (filter + main)
  
  # Rationale context: Persona (~1500 tok) + working memory (~800 tok) + 
  # semantic memory (~300 tok) + system (~200 tok) = ~2800 tok tối thiểu.
  # 2048 không đủ chứa, sẽ phải truncate persona → làm hỏng nhân vật.
  # Pre-flight Day 1 PHẢI confirm: warm TTFT với prompt-cache < 500ms.

llm_fallback:  # Emergency path — KHÔNG chạy process riêng, trỏ chung sang llm_filter (xem cảnh báo VRAM bên dưới)
  provider: llama_cpp
  shares_instance_with: llm_filter   # cùng port 8082, gọi với prompt khác (fallback response thay vì filter check)
  context_size: 2048
  activated_when: "primary_timeout OR primary_oom"

llm_filter:
  provider: llama_cpp
  binary: .\llama.cpp\build\bin\Release\llama-server.exe
  model_path: .\models\llm\gemma-4-e4b-Q4_K_M.gguf
  port: 8082
  context_size: 2048
  gpu_layers: 999
  kv_cache_type_k: q8_0
  kv_cache_type_v: q8_0
```

⚠️ **Lưu ý VRAM quan trọng (khác với thiết kế Ollama ban đầu):** `llm_fallback` và `llm_filter` đều cần model `gemma-4-e4b`. Nếu mỗi cái chạy 1 `llama-server` process riêng (port khác nhau), **model E4B bị load vào VRAM 2 lần** (~3GB × 2 = 6GB), không share được như giả định "filter share với QC" ở Section 3.2/4.3.

**Mặc định trong config trên: dùng chung 1 instance** (`llm_fallback.shares_instance_with: llm_filter`) — chỉ 1 process E4B chạy trên port 8082, cả logic filter và logic fallback đều gọi chung endpoint này với prompt khác nhau. Tiết kiệm 3GB, đúng tinh thần thiết kế gốc. `FallbackManager` (Section 8.7.7) gọi cùng 1 client tới port 8082 cho cả 2 mục đích.

**Chỉ tách thành 2 process riêng nếu:** VRAM Pre-flight Day 1 cho thấy còn dư nhiều (>6GB), và bạn muốn `llm_fallback` sẵn sàng ngay cả khi `llm_filter` đang bận xử lý request khác (tránh block lẫn nhau khi có nhiều request đồng thời).
  
stt:
  provider: faster_whisper  # ← v2: đổi từ whisper
  model: small              # ← v2: đổi từ base
  language: vi
  device: cuda
  compute_type: int8_float16  # faster-whisper optimization
  
stt_fallback:  # ← MỚI
  provider: faster_whisper
  model: tiny
  device: cpu
  
tts:
  provider: piper  # ← Xác nhận sau Pre-flight Day 2
  # Alternative options nếu Piper không đủ:
  # provider: xtts
  # provider: vixtts
  model_path: .\models\tts\piper_vi_female.onnx
  device: cpu
  sample_rate: 22050
  
tts_fallback:  # ← MỚI
  provider: piper
  model_path: .\models\tts\piper_vi_backup.onnx
  device: cpu

embedding:
  provider: sentence_transformers
  model: BAAI/bge-m3
  device: cpu
  cache_size: 1000  # Cache recent embeddings
```

### 10.4. triggers.yaml ⭐ **MỚI**

```yaml
triggers:
  # Priority weights
  priorities:
    operator_voice_interrupt: 150
    operator_voice: 100
    chat_donation: 90
    chat_subscribe: 85
    chat_mention: 80
    chat_question: 70
    chat_keyword: 60
    chat_normal: 40
    continuation: 30
    ambient_talk: 20
    scheduled: 10
  
  # Rate limiting
  rate_limits:
    chat_normal:
      window_seconds: 10
      max_responses: 3
    chat_mention:
      window_seconds: 10
      max_responses: 5
  
  # Queue settings
  queue:
    max_size: 30
    default_ttl_seconds: 30
    drop_policy: lowest_priority
  
  # Keywords Mai reacts to
  chat_keywords:
    - mai
    - "mèo"  # nếu Mai có chuyện với mèo
    - "ông"
  
  # Spam detection
  spam:
    max_emoji_ratio: 0.7
    max_repeat_chars: 5
    blocked_patterns:
      - "^k+$"       # kkkkk
      - "^h+a+$"     # hahaha
```

### 10.5. state_machine.yaml ⭐ **MỚI**

```yaml
state_machine:
  initial_state: IDLE
  
  cooldown_ms: 500
  
  interrupt_policy:
    operator_voice:
      allow_after_ms: 2000  # sau 2s speaking, mới allow interrupt
    operator_voice_interrupt:
      allow_after_ms: 0     # allow ngay
    chat_donation:
      allow_after_ms: 999999  # không interrupt speech vì chat
  
  auto_recovery:
    stuck_state_check_interval_seconds: 1
    # v2.1: không có state ERROR riêng — llm_fail transition thẳng
    # THINKING → COOLDOWN với fallback response, xem Section 8.7.1
```

---

## 11. DEVELOPMENT ROADMAP (v2 - REALISTIC)

### 11.0. Pre-flight Sprint (Ngày 1-4) ⭐ **BẮT BUỘC**

Xem Section 0.

**Definition of Done:**
- [ ] Spike Day 1 report với LLM benchmark
- [ ] Spike Day 2 report với TTS choice
- [ ] Spike Day 3 report với STT accuracy
- [ ] Section 10.3 (models.yaml) updated with real choices
- [ ] Trade-off log updated

### 11.1. Phase 0: Foundation (Tuần 1-3) 

**Timeline updated:** 2 → 3 tuần (buffer for WebSocket + dashboard debugging)

**Goal:** Setup + infrastructure trước khi có bất kỳ feature nào.

**Deliverables:**
- [ ] Repo structure với module layout
- [ ] Feature registry hoạt động
- [ ] Dashboard skeleton (WebSocket + FastAPI + HTML)
- [ ] Metrics collector với 3 metric giả
- [ ] Emergency stop hotkey
- [ ] Config loader với hot-reload
- [ ] Logger setup (structlog + JSONL)
- [ ] SQLite schema initialized
- [ ] **State machine framework** ⭐
- [ ] **Trigger Manager skeleton** ⭐
- [ ] **Fallback Manager skeleton** ⭐

**Definition of Done:**
- [ ] Dashboard mở được ở localhost:8080
- [ ] Toggle giả bật/tắt được (chưa làm gì)
- [ ] Metric giả cập nhật realtime trên chart
- [ ] Emergency stop hotkey (Ctrl+Shift+X) hoạt động
- [ ] State machine transitions log được
- [ ] Config file reload không cần restart
- [ ] Không có memory leak sau 1 giờ chạy idle

### 11.2. Phase 1: Core LLM (Tuần 4-6)

**Timeline updated:** 2 → 3 tuần (persona prompt engineering)

**Goal:** Mai text-only, no voice, no TTS.

**Deliverables:**
- [ ] llama-server compiled/available + Gemma 12B GGUF loaded
- [ ] LLM service với streaming
- [ ] Prompt manager + persona v1
- [ ] Parser (regex + Pydantic)
- [ ] CLI input mode
- [ ] LLM metrics
- [ ] Dashboard hiển thị LLM metrics
- [ ] **LLM Fallback chain (Level 1-3)** ⭐

**Definition of Done:**
- [ ] Gõ chat vào CLI, Mai trả lời với persona **subjectively đúng chất** qua 20 turn test
- [ ] Format block mood parse thành công > 95%
- [ ] TTFT P50 < target (từ Pre-flight)
- [ ] Không crash sau 100 turn liên tục
- [ ] Fallback triggered được khi force LLM timeout
- [ ] Dashboard hiển thị TTFT/decode speed realtime

### 11.3. Phase 2: Trigger & State Machine (Tuần 7-8) ⭐ **MỚI PHASE**

**Goal:** Business logic cho turn-taking.

**Deliverables:**
- [ ] Trigger Manager với priority queue
- [ ] Trigger classification (12+ types)
- [ ] Rate limiting per source
- [ ] State machine full implementation
- [ ] Interrupt policy
- [ ] Ambient talk trigger
- [ ] Dashboard: Triggers tab
- [ ] Dashboard: State Machine tab

**Definition of Done:**
- [ ] Chat spam simulator: 60 tin/phút, Mai không respond tất cả
- [ ] Priority correct: operator voice > donation > mention > normal
- [ ] State transitions log đầy đủ
- [ ] Ambient talk trigger sau silence > 1 phút
- [ ] Watchdog detect deadlock khi force stuck state

### 11.4. Phase 3: Filter (Tuần 9-10)

**Goal:** Rule-based filter + regenerate logic.

**Deliverables:**
- [ ] Rule filter với 4 category
- [ ] Regenerate với hint
- [ ] Filter metrics
- [ ] Dashboard filter tab
- [ ] **Filter Fallback chain (fail-open)** ⭐

**Definition of Done:**
- [ ] Test 20 troll attempts: catch rate > 80%
- [ ] Test 100 normal outputs: false positive < 5%
- [ ] Regenerate hoạt động khi persona break detected
- [ ] Filter fail-open khi regex error (log warning nhưng allow)

### 11.5. Phase 4: TTS (Tuần 11-14)

**Timeline updated:** 2 → 4 tuần (streaming + interrupt là rabbit hole)

**Goal:** Text → audio playback với streaming.

**Deliverables:**
- [ ] TTS setup (từ Pre-flight Day 2 decision)
- [ ] Streaming TTS pipeline
- [ ] Audio playback thứ tự
- [ ] TTS metrics
- [ ] Interrupt handling (integrate với state machine)
- [ ] **TTS Fallback chain** ⭐
- [ ] **Subtitle overlay** (level 3 fallback) ⭐

**Definition of Done:**
- [ ] **Must-have:** Mai "nói" được, TTFA P50 < 1s
- [ ] **Must-have:** Không audio overlap giữa turns
- [ ] **Stretch:** Interrupt mượt (fade 200ms, không giật)
- [ ] **Stretch:** Subtitle sync với audio
- [ ] TTS quality subjective > 6/10 qua 30 câu test

### 11.6. Phase 5: Voice input (Tuần 15-17)

**Timeline updated:** 2 → 3 tuần

**Goal:** Ông nói được với Mai.

**Deliverables:**
- [ ] faster-whisper small setup
- [ ] Streaming STT
- [ ] Echo cancellation
- [ ] Voice activity detection
- [ ] **STT Fallback chain** ⭐

**Definition of Done:**
- [ ] Nói câu bình thường: WER < 20% (từ Pre-flight)
- [ ] Không loop (Mai không nghe chính mình)
- [ ] VAD trigger đúng lúc bắt đầu/kết thúc speech
- [ ] STT chunks streaming đến LLM < 300ms delay

### 11.7. Phase 6: Animation (Tuần 18-20)

**Timeline updated:** 2 → 3 tuần (VTube Studio quirks)

**Goal:** Mai có biểu cảm mood.

**Deliverables:**
- [ ] VTube Studio integration
- [ ] Mood → expression mapping
- [ ] Sync với TTS
- [ ] **Animation Fallback (static image)** ⭐

**Definition of Done:**
- [ ] **Must-have:** 5 mood chính có expression tương ứng
- [ ] **Must-have:** Auto-reconnect khi VTube Studio disconnect
- [ ] **Stretch:** Smooth interpolation giữa expressions
- [ ] **Stretch:** Lip-sync với audio

### 11.8. Phase 7: Memory (Tuần 21-23)

**Timeline updated:** 2 → 3 tuần

**Goal:** Mai nhớ chuyện dài hạn.

**Deliverables:**
- [ ] SQLite + sqlite-vec setup
- [ ] bge-m3 embedding (CPU)
- [ ] Semantic memory service (với 150ms timeout)
- [ ] Manual memory injection UI
- [ ] Auto memory extraction
- [ ] Multi-viewer profile
- [ ] **Memory Fallback (working-only)** ⭐

**Definition of Done:**
- [ ] Memory retrieve trong 150ms P95
- [ ] Fallback về working memory nếu timeout
- [ ] Manual inject 10 memories, Mai callback được > 80%
- [ ] Multi-viewer: Mai nhớ 5 viewer thường xuyên qua sessions

### 11.9. Phase 8: QC + Data pipeline (Tuần 24-26)

**Timeline updated:** 2 → 3 tuần

**Deliverables:**
- [ ] Persona QC async
- [ ] Data collector auto-scoring
- [ ] Review UI với **Alpine.js** ⭐
- [ ] Export training dataset

**Definition of Done:**
- [ ] Sau 1 tuần chạy: 500+ mẫu ranked sẵn
- [ ] Review UI: approve/reject/edit hoạt động
- [ ] Export JSONL format cho Unsloth
- [ ] QC score correlate với subjective quality (spot check 50 samples)

### 11.10. Phase 9: First fine-tune (Tuần 27-28)

**Timeline updated:** 2 tuần

**Deliverables:**
- [ ] Unsloth setup
- [ ] Training script (SFT hoặc DPO)
- [ ] Eval script với hold-out test set
- [ ] Deploy fine-tuned model
- [ ] A/B test v1 vs v2

**Definition of Done:**
- [ ] Model v2 fine-tuned, deploy được
- [ ] Persona QC score cao hơn v1 measurably (delta > 0.5)
- [ ] Không catastrophic forgetting (test general knowledge)
- [ ] Rollback plan working

### 11.11. Phase 10+: Iteration

Sau Phase 9, cycle iterative không có deadline cứng.

### 11.12. Timeline Summary (v2)

| Phase | Duration | Cumulative | Notes |
|---|---|---|---|
| Pre-flight | 4 days | 4 days | ⚡ Critical |
| Phase 0 | 3 weeks | 4 weeks | Infrastructure |
| Phase 1 | 3 weeks | 7 weeks | LLM |
| Phase 2 | 2 weeks | 9 weeks | Triggers ⭐ |
| Phase 3 | 2 weeks | 11 weeks | Filter |
| Phase 4 | 4 weeks | 15 weeks | TTS |
| Phase 5 | 3 weeks | 18 weeks | STT |
| Phase 6 | 3 weeks | 21 weeks | Animation |
| Phase 7 | 3 weeks | 24 weeks | Memory |
| Phase 8 | 3 weeks | 27 weeks | QC + Data |
| Phase 9 | 2 weeks | 29 weeks | Fine-tune |

**Total: ~29 tuần (7 tháng) part-time solo dev đến Phase 9.**

So với v1.0: 18 tuần → 29 tuần. Mental prepare cho reality.

---

## 12. TESTING STRATEGY

*(Giữ nguyên từ v1.0, thêm:)*

### 12.6. State Machine Tests ⭐ **MỚI**

**Property-based tests với hypothesis:**
- Random sequence of triggers → state machine always valid
- Deadlock detection working
- Emergency stop từ mọi state → PAUSED

**Fixture: `state_scenarios.yaml`:** *(v2.1: cập nhật theo 5 states)*
```yaml
scenarios:
  - name: normal_flow
    triggers: [chat_mention]
    expected_states: [IDLE, THINKING, SPEAKING, COOLDOWN, IDLE]
  - name: interrupt_flow
    triggers: [chat_mention, operator_voice_after_1s]
    expected_states: [IDLE, THINKING, SPEAKING, COOLDOWN, THINKING, SPEAKING, COOLDOWN, IDLE]
    expected_flags: {turn_1: {interrupted: true}}
  # ... thêm scenarios khi tìm thấy edge case mới qua chạy thật
```

### 12.7. Trigger Manager Tests ⭐ **MỚI**

- Priority correctness: given events, verify ordering
- Rate limiting: burst 100 events, verify max N respond
- Spam detection: known spam patterns → SKIP
- Ambient talk trigger: silence > threshold → trigger created

---

### 12.8. Trigger + State Machine Integration Tests ⭐ **MỚI (v2.1)**

**Lý do cần riêng:** Section 12.6 test state machine riêng, phần Testing khác test trigger riêng — nhưng bug thực sự sống ở **interaction** giữa 2 hệ thống này (race condition, timing edge case).

```python
# tests/integration/test_trigger_state_interaction.py
import pytest
import asyncio

@pytest.mark.asyncio
class TestTriggerStateInteraction:
    
    async def test_trigger_during_thinking_queued(self, orch):
        """Trigger đến khi đang THINKING → queue, không interrupt."""
        await orch.send_event(chat_event("hi mai"))
        await orch.wait_for_state("THINKING")
        
        await orch.send_event(chat_event("hello mai"))
        
        assert orch.state == "THINKING"
        assert orch.trigger_queue_size == 1
        
        await orch.wait_for_state("COOLDOWN")
        await orch.wait_for_state("THINKING")
        assert orch.current_trigger.content == "hello mai"
    
    async def test_operator_voice_interrupts_speaking(self, orch):
        """Operator voice → interrupt SPEAKING."""
        await orch.send_event(chat_event("kể chuyện"))
        await orch.wait_for_state("SPEAKING")
        await asyncio.sleep(2)
        assert orch.state == "SPEAKING"
        
        await orch.send_event(voice_event("stop"))
        await asyncio.sleep(0.3)  # fade time
        
        assert orch.state in ["COOLDOWN", "THINKING"]
        assert orch.last_turn.interrupted == True
    
    async def test_spam_during_speaking_dropped(self, orch):
        """Chat spam khi Mai đang nói → drop, không queue."""
        await orch.send_event(chat_event("mai ơi"))
        await orch.wait_for_state("SPEAKING")
        
        for _ in range(20):
            await orch.send_event(chat_event("kkkkk"))
        
        assert orch.trigger_queue_size == 0
    
    async def test_ambient_talk_after_silence(self, orch):
        """Silence > 60s → ambient talk trigger."""
        orch.trigger_manager.last_speak_time = datetime.now() - timedelta(seconds=61)
        trigger = await orch.trigger_manager.get_next_trigger()
        assert trigger is not None
        assert trigger.type == TriggerType.AMBIENT_TALK
    
    async def test_emergency_stop_from_speaking(self, orch):
        """Emergency stop từ SPEAKING → PAUSED ngay."""
        await orch.send_event(chat_event("hello"))
        await orch.wait_for_state("SPEAKING")
        
        await orch.emergency_stop()
        
        assert orch.state == "PAUSED"
        assert orch.tts_service.is_playing == False
    
    async def test_race_trigger_during_state_transition(self, orch):
        """Race: 2 trigger đến gần như đồng thời."""
        await orch.send_event(chat_event("hi"))
        await asyncio.sleep(0.001)
        await orch.send_event(chat_event("mai"))
        
        await asyncio.sleep(10)
        
        assert orch.processed_turns >= 2
        assert orch.trigger_queue_size == 0
    
    async def test_cooldown_prevents_immediate_next(self, orch):
        """COOLDOWN 500ms prevent immediate next turn."""
        await orch.send_event(chat_event("hi"))
        await orch.wait_for_state("COOLDOWN")
        
        cooldown_start = time.time()
        await orch.send_event(chat_event("mai"))
        
        await asyncio.sleep(0.1)
        assert orch.state == "COOLDOWN"
        
        await orch.wait_for_state("THINKING")
        elapsed = time.time() - cooldown_start
        assert elapsed >= 0.5
    
    async def test_state_stuck_recovery(self, orch):
        """State stuck > threshold → watchdog auto recover."""
        orch.llm_service.simulate_hang = True
        
        await orch.send_event(chat_event("hi"))
        await orch.wait_for_state("THINKING")
        
        await asyncio.sleep(11)  # vượt threshold 10s cho THINKING
        
        assert orch.state != "THINKING"
```

**Fixture setup trong `conftest.py`:**

```python
@pytest.fixture
async def orch():
    orchestrator = Orchestrator(
        llm=MockLLMService(),
        tts=MockTTSService(),
        stt=MockSTTService(),
        config=test_config()
    )
    await orchestrator.start()
    yield orchestrator
    await orchestrator.stop()
```

---

## 13. DEPLOYMENT & OPERATIONS

### 13.1. Runtime environment

**OS:** Windows 11 (target duy nhất — không dual-support Linux)

**Dependencies:**

```
Python 3.11+
CUDA 12.x (matching driver)
llama.cpp (đã build sẵn với GGML_CUDA=ON)
Piper TTS binary (hoặc XTTS/viXTTS tuỳ Pre-flight)
VTube Studio (nếu dùng)
OBS Studio (recording/streaming)
```

**Python packages (requirements.txt):**

```
fastapi>=0.110
uvicorn[standard]>=0.29
websockets>=12
pydantic>=2.6
pydantic-settings>=2.2
structlog>=24.1
prometheus-client>=0.20
sqlite-vec>=0.1
httpx>=0.27              # gọi llama-server OpenAI-compatible API
openai-whisper>=20231117 # hoặc faster-whisper (khuyến nghị, xem 10.3)
faster-whisper>=1.0
piper-tts>=1.2
sentence-transformers>=2.7
numpy>=1.26
sounddevice>=0.4
pyaudio>=0.2             # Windows: nếu `pip install pyaudio` báo lỗi build wheel → tải wheel từ https://www.lfd.uci.edu/~gohlke/pythonlibs/ hoặc dùng `pipwin install pyaudio`
python-vtube-studio>=0.5
watchdog>=4.0
keyboard>=0.13.5         # global hotkey cho emergency stop (Windows: cần chạy Python với quyền admin để hook toàn cục)
pytest>=8.0
pytest-asyncio>=0.23
hypothesis>=6.100
```

*(Không cần package `ollama` — giao tiếp llama-server qua `httpx` tới OpenAI-compatible endpoint.)*

**Windows-specific notes (đọc trước khi code Phase 0):**

- **PowerShell execution policy:** nếu `Activate.ps1` bị chặn, chạy 1 lần: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
- **Long path support:** Windows mặc định giới hạn path 260 ký tự. Repo Mai với đường dẫn `models/llm/...` có thể chạm giới hạn khi nested sâu. Enable long path support: `New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force`
- **Global hotkey** (`keyboard` lib) cần Python chạy với quyền **Administrator** để hook được phím toàn cục. Chạy `start.ps1` với "Run as Administrator" hoặc dùng Task Scheduler.
- **Firewall:** lần đầu chạy `llama-server.exe` và FastAPI dashboard, Windows Defender sẽ hỏi cho phép listen port. Chọn "Private networks" là đủ (không cần Public trừ khi muốn stream từ máy khác trong LAN).
- **CUDA & Visual Studio:** llama.cpp build với CUDA cần đúng version VS Build Tools khớp với CUDA toolkit — nếu bạn đã build được rồi thì bỏ qua, đây là note cho tương lai khi update CUDA/VS.
- **Antivirus có thể chặn compile output:** một số AV (Bitdefender, Kaspersky) cờ `.exe` mới build từ llama.cpp là suspicious. Add exception cho folder `llama.cpp\build\` nếu gặp.

### 13.2. Startup sequence

```
1. Load config files
2. Initialize logger + metrics
3. Start dashboard server (available even nếu core fail)
4. Start llama-server instances (main, shared_e4b) — xem Section 8.2 process_manager
5. Wait healthy check cho từng instance (timeout 30s)
6. Load enabled features
7. Start core services (LLM client, parser)
8. Start optional services (per feature toggle)
9. Register health checks
10. Start event bus
11. Ready state
```

Startup log rõ ràng, dashboard hiển thị progress. Bước 4-5 là điểm khác biệt so với dùng Ollama — llama-server không chạy sẵn như daemon, orchestrator phải tự spawn process và đợi model load xong trước khi nhận trigger đầu tiên.

### 13.3. Graceful shutdown (Windows)

> **Khác biệt quan trọng với Linux:** Windows không có tín hiệu SIGTERM "graceful" cho process bất kỳ như POSIX. Python `subprocess.Popen.terminate()` trên Windows gọi thẳng `TerminateProcess()` — tương đương hard-kill, không có cơ hội cho process tự dọn dẹp trừ khi nó tự bắt `CTRL_BREAK_EVENT` (cần tạo process với cờ `CREATE_NEW_PROCESS_GROUP`). Vì `llama-server.exe` không tự implement graceful shutdown, chấp nhận hard-kill cho nó là hợp lý — nó không giữ state quan trọng giữa các request.

```
1. Nhận Ctrl+C (KeyboardInterrupt, hoạt động cross-platform trong Python) 
   hoặc lệnh "Shutdown" từ dashboard
2. Stop accepting new events
3. Drain in-flight LLM calls (max 5s) — đánh dấu turn dở dang trong log, KHÔNG đợi llama-server tự kết thúc
4. Save current state to disk
5. Stop services in reverse order
6. Terminate llama-server processes:
   proc.terminate()  # TerminateProcess trên Windows — hard-kill ngay, không có bước "chờ rồi SIGKILL"
7. Close DB connections
8. Save metrics summary
9. Exit
```

**Code:**

```python
import signal

def register_shutdown_handler(orchestrator):
    # KeyboardInterrupt (Ctrl+C) hoạt động cross-platform kể cả Windows
    signal.signal(signal.SIGINT, lambda sig, frame: asyncio.create_task(orchestrator.shutdown()))

async def shutdown(self):
    self.accepting_events = False
    await self._drain_inflight(timeout=5)
    await self._save_state()
    for name, proc in self.llama_processes.items():
        proc.terminate()  # Windows: TerminateProcess, không graceful nhưng đủ dùng cho llama-server
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()  # force, hiếm khi cần vì terminate() đã là hard-kill trên Windows
    await self._close_db()
```

### 13.4. Backup

**Daily automated:**
- SQLite databases → `backups/YYYY-MM-DD/`
- Config files → `backups/YYYY-MM-DD/config/`
- Persona versions → `backups/YYYY-MM-DD/persona/`
- `--prompt-cache` files → không cần backup (rebuild nhanh từ persona.md)

**Retention:** 30 ngày local, có thể mở rộng.

### 13.5. Recovery

Trường hợp crash:
1. Restart script (`scripts/start.ps1`) — script này gọi `LlamaServerProcessManager.start_all()` trước khi khởi động orchestrator
2. Auto-detect last state từ SQLite
3. Restore feature toggles
4. Restore working memory (last 20 turns)
5. Report incident vào dashboard log

### 13.6. Monitoring health

Health check mỗi 10s (xem `LlamaServerHealthMonitor` ở Section 13.7):
- Cả 2 llama-server instance (main, shared_e4b) responsive qua `/health`?
- VRAM usage bình thường?
- No stuck LLM calls?
- Event queue không phình?

Nếu unhealthy → alert dashboard + auto-recovery attempt.

### 13.7. Component Crash Recovery ⭐ **MỚI**

**llama-server crash (thường xảy ra khi OOM):**

Vì có 3 instance (`main`, `fallback`, `filter`) chạy độc lập trên 3 port, monitor phải theo dõi từng cái riêng — 1 instance chết không nhất thiết kéo theo cái khác.

```python
class LlamaServerHealthMonitor:
    def __init__(self, instances: dict[str, LlamaServerConfig]):
        # instances = {"main": cfg_8080, "fallback": cfg_8081, "filter": cfg_8082}
        self.instances = instances
        self.processes: dict[str, subprocess.Popen] = {}
    
    async def monitor(self):
        while True:
            await asyncio.sleep(5)
            for name, cfg in self.instances.items():
                try:
                    await self._ping(cfg.port)
                except Exception:
                    logger.error("llama_server_unresponsive", instance=name)
                    await self._restart(name, cfg)
    
    async def _ping(self, port: int):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://localhost:{port}/health", timeout=3)
            resp.raise_for_status()
    
    async def _restart(self, name: str, cfg: LlamaServerConfig):
        # Nếu là instance "main" → cần emergency stop vì đây là critical path
        if name == "main":
            await self.state_machine.emergency_stop()
        
        # Kill process cũ (nếu còn sống)
        old_proc = self.processes.get(name)
        if old_proc and old_proc.poll() is None:
            old_proc.terminate()
            await asyncio.sleep(2)
            if old_proc.poll() is None:
                old_proc.kill()
        
        # Restart với đúng flags từ models.yaml
        new_proc = subprocess.Popen(cfg.to_cli_args())
        self.processes[name] = new_proc
        await asyncio.sleep(5)  # llama-server load model cần vài giây
        
        # Verify healthy
        for _ in range(15):  # llama.cpp load model chậm hơn Ollama do không cache sẵn
            try:
                await self._ping(cfg.port)
                logger.info("llama_server_restarted", instance=name)
                if name == "main":
                    await self.state_machine.recover()
                return
            except Exception:
                await asyncio.sleep(1)
        
        logger.critical("llama_server_restart_failed", instance=name)
        # Alert dashboard, no auto-retry (tránh restart loop tốn VRAM)
```

**Lưu ý khác với Ollama:** llama.cpp không có daemon quản lý sẵn như Ollama — restart nghĩa là load lại toàn bộ .gguf từ đĩa, chậm hơn (5-15s tuỳ kích thước model), và mất `--prompt-cache` đã build (phải rebuild cache sau restart, TTFT lượt đầu sau khi restart sẽ cao hơn bình thường).

**VTube Studio disconnect:**

Auto-reconnect với exponential backoff, log alerts to dashboard.

**Dashboard WebSocket disconnect:**

Không critical, dashboard tự reconnect. Mai vẫn chạy.

### 13.8. Log rotation ⭐ **MỚI**

```yaml
# config/logging.yaml
rotation:
  turns:
    when: midnight
    interval: 1
    keep_days: 30
    compress_after_days: 7
  system:
    when: midnight
    keep_days: 14
  errors:
    when: midnight
    keep_days: 90  # Errors keep lâu hơn
```

Auto-cleanup script chạy nightly, dashboard hiển thị disk usage.

---

## APPENDIX A: FILE STRUCTURE (updated)

```
mai/
├── config/
│   ├── system.yaml
│   ├── features.yaml
│   ├── models.yaml
│   ├── triggers.yaml       ← MỚI
│   ├── state_machine.yaml  ← MỚI
│   ├── logging.yaml        ← MỚI
│   ├── persona/
│   └── prompts/
├── spike/                   ← MỚI (Pre-flight)
│   ├── day1_llm_latency/
│   ├── day2_tts_quality/
│   ├── day3_stt_accuracy/
│   └── day4_e2e/
├── data/
├── logs/
├── models/
├── interfaces/
│   ├── base.py
│   ├── input.py
│   ├── stt.py
│   ├── llm.py
│   ├── filter.py
│   ├── tts.py
│   ├── animation.py
│   ├── memory.py
│   ├── trigger.py          ← MỚI
│   └── state.py            ← MỚI
├── services/
├── orchestrator/
│   ├── main.py
│   ├── features.py
│   ├── event_bus.py
│   ├── health_monitor.py
│   ├── metrics_collector.py
│   ├── dashboard_server.py
│   ├── config_loader.py
│   ├── state_machine.py    ← MỚI
│   ├── trigger_manager.py  ← MỚI
│   └── fallback_manager.py ← MỚI
├── dashboard/
├── scripts/
├── tests/
├── docs/
├── .env.example
├── requirements.txt
└── README.md
```

---

## APPENDIX B: KHỞI ĐỘNG NHANH (Windows / PowerShell)

**Tuần 0 (Pre-flight):**
```powershell
mkdir mai; cd mai
git init
python -m venv venv
.\venv\Scripts\Activate.ps1

# Clone spike templates
mkdir spike\day1, spike\day2, spike\day3, spike\day4

# Day 1: LLM benchmark
cd spike\day1
# Build/verify llama.cpp CUDA (đã có sẵn), tải Gemma GGUF, chạy llama-server.exe
# Run benchmark.py
# Write day1_report.md

# Day 2: TTS testing
cd ..\day2
# Download Piper voices, XTTS, ...
# Test synthesis
# Write day2_report.md

# Day 3: STT testing
cd ..\day3
# Install faster-whisper
# Record test samples
# Compute WER
# Write day3_report.md

# Day 4 (optional): E2E
cd ..\day4
# Wire together minimum viable
# Measure latency
# Write day4_report.md
```

> **Lưu ý PowerShell execution policy:** nếu `Activate.ps1` bị chặn chạy, cần set 1 lần: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

**Sau Pre-flight, cập nhật `ARCHITECTURE.md` với real numbers, rồi tiến Phase 0.**

---

## APPENDIX C: TRADE-OFF DECISIONS LOG (v2.1)

| Decision | Chọn | Bỏ | Lý do |
|---|---|---|---|
| **OS target** ⭐ v2.2 | **Windows 11** | Dual (Windows/Ubuntu) | VTube Studio native tốt trên Windows; ecosystem VTuber phần lớn dùng Windows; single target giảm complexity code (không phải if/else theo platform) |
| LLM backend | llama.cpp (llama-server) ⭐ v2.2 | Ollama | Đã có sẵn build; control trực tiếp KV cache Q8, speculative decoding, prompt caching — đúng 3 optimization đã đặt ra, không qua abstraction |
| TTS primary | *TBD from Pre-flight* | - | Quyết định sau Day 2, điền vào `decisions/002_tts_choice.md` |
| STT primary | faster-whisper small | Whisper base | Whisper base WER quá cao cho tiếng Việt |
| Vector store | SQLite+vec | Qdrant/Chroma | Không cần server riêng |
| Dashboard (Phase 0-5) | Vanilla JS | React | Đơn giản cho solo dev |
| Dashboard (Phase 6+) | Alpine.js | Vanilla JS | Cần reactivity cho review UI |
| Metrics | prometheus_client | Custom | Chuẩn ngành |
| Persona filter primary | Rule-based | AI-based | VRAM tight, rule bắt được 80% |
| Filter policy | Fail-open | Fail-closed | VTuber giải trí, im lặng tệ hơn 1 câu chưa lọc kỹ |
| Communication internal | asyncio queue | Redis | Single machine |
| Feature toggle storage | YAML | DB | Human-readable |
| **Context size** | **4096** ⭐ v2.1 fix | 2048 | v2.0 sai: 2048 không đủ chứa persona(1500)+memory(800)+system(200)≈2800tok. TTFT optimize bằng prompt caching, không cắt context |
| Latency target | P50/P95 | Cứng 1.5s | Realistic hơn |
| Primary UX metric | TTFA | Total latency | User cảm nhận qua bắt đầu nghe |
| **Trigger types (Phase 2 initial)** | **4 types** ⭐ v2.1 | 12 types | YAGNI — Neuro không cần scoring 12 tầng, grow khi có tín hiệu thật |
| **Conversation states** | **5 states** ⭐ v2.1 | 8 states | Gộp LISTENING vào THINKING, INTERRUPTED thành flag, ERROR dùng exception handling |
| **Fallback levels** | **2 levels** ⭐ v2.1 | 3-4 levels | Level 2 cũ (model dự phòng) hiếm khi hữu ích hơn canned response; đơn giản hoá |
| **Circuit breaker** | **Không dùng ban đầu** ⭐ v2.1 | Circuit breaker pattern | Timeout + try/except đủ cho MVP; add khi thấy fail lặp lại gây tốn tài nguyên |
| **Migration tool** | **Versioned SQL scripts** ⭐ v2.1 | Alembic | Nhẹ hơn, đủ cho solo dev, dễ đọc trực tiếp |

---

## APPENDIX D: PRE-FLIGHT CHECKLIST ⭐ **MỚI**

Trước khi bắt đầu Phase 0, verify:

**Hardware:**
- [ ] RTX 5060 Ti 16GB confirmed working
- [ ] CUDA driver installed và test
- [ ] `nvidia-smi` chạy được
- [ ] VRAM baseline (idle) < 500MB

**Software:**
- [ ] llama.cpp built với CUDA support (`GGML_CUDA=ON`), `llama-server.exe` chạy được từ `.\build\bin\Release\`
- [ ] Model GGUF đã tải (main 12B + E4B cho fallback/filter)
- [ ] Python 3.11+ với venv setup (`.\venv\Scripts\Activate.ps1` chạy được)
- [ ] Git repo initialized

**Windows setup (Section 13.1):**
- [ ] PowerShell execution policy đã set `RemoteSigned` (scope CurrentUser)
- [ ] Long path support enabled trong registry
- [ ] Windows Defender/AV đã exception folder `llama.cpp\build\`
- [ ] Test được Python chạy `keyboard` lib với quyền Administrator (cho emergency hotkey)

**Pre-flight results:**
- [ ] Day 1: LLM latency benchmark done, Go/No-go decision made
- [ ] Day 2: TTS Vietnamese quality validated, primary + fallback chosen
- [ ] Day 3: STT accuracy validated, model size decided
- [ ] Day 4 (optional): E2E latency estimate done

**Documentation:**
- [ ] Section 10.3 (models.yaml) updated với real choices
- [ ] Appendix C updated với new decisions
- [ ] Section 1.1 (latency target) updated với measured baseline

**Mental preparation:**
- [ ] Chấp nhận timeline ~29 tuần cho full stack
- [ ] Chấp nhận có thể trade quality vs latency
- [ ] Chấp nhận có thể phá "100% local" nếu TTS Vietnamese không đủ tốt

**Nếu tất cả checked → tiến Phase 0.**

**Nếu có ⚠️ trong pre-flight results → dừng lại, re-plan trước khi commit vào code.**

---

## KẾT

Đây là bộ tài liệu v2.0 sau khi tích hợp review feedback. Điểm quan trọng cần nhớ:

**1. Pre-flight không phải optional.** 4 ngày spike ROI cao hơn 4 tuần refactor.

**2. Phase 0 quan trọng nhất.** Infrastructure trước feature.

**3. Trigger & State Machine là business logic cốt lõi.** Không phải "thêm sau".

**4. Fallback everywhere.** Mỗi module critical có ít nhất 1 fallback.

**5. TTFA > Total Latency.** UX perception qua bắt đầu nghe.

**6. Timeline realistic.** 29 tuần, không phải 18 tuần. Mental prepare.

**7. Toggle everything toggleable.** Enable/disable qua dashboard runtime.

**8. Metrics từ ngày 1.** Không chờ debug mới add.

**9. Persona is sacred.** Fine-tune data align với persona core.

**10. Validate before build.** Không commit vào assumption chưa test.
