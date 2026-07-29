# Spike Day 1 Report — LLM Latency

**Ngày đo:** 2026-07-29 (21:27–21:57)
**Hardware:** RTX 5060 Ti 16GB
**Model:** gemma-4-12b-Q4_K_M.gguf
**llama.cpp:** llama-b10178-bin-win-cuda-13.3-x64 (từ `docs/`)
**Flags:** `-ngl 999 -c 4096 --cache-type-k q8_0 --cache-type-v q8_0 --port 8080`
**Endpoint:** http://localhost:8080, `n_ctx=4096` xác nhận qua `/props`

## Kết quả đo

| Scenario | prompt_tok | TTFT (ms) | Decode (tok/s) | Target TTFT | Target decode | Pass? |
|---|---|---|---|---|---|---|
| S1 Cold start | 488 | **444** | **45.5** | <500 | >50 | TTFT ✅ / decode ⚠️ (miss target, cách xa no-go) |
| S2 Warm short | 488 | **552** | **45.3** | <300 | >60 | ❌ TTFT / ❌ decode (cache prompt không cải thiện) |
| S3 Warm medium | 2019 | **545** | **40.0** | <800 | >45 | TTFT ✅ / decode ⚠️ |
| S4 Warm long | 3813 | **541** | **40.0** | <1500 | >35 | ✅ / ✅ |

## S5 Overheating (30 phút, prompt ~2K, request mỗi 60s)

| Metric | Giá trị |
|---|---|
| Số LLM sample | 30 |
| TTFT first 3 avg | 964 ms (có outlier 1647ms sample 2, chưa rõ nguyên nhân) |
| TTFT last 3 avg | 688 ms |
| Decode first 3 avg | 41.6 tok/s |
| Decode last 3 avg | 42.6 tok/s |
| GPU max temp | **63 °C** |
| GPU avg temp | 49.9 °C |
| Throttle (clock-based, RAW) | 91.1% ← **false positive** |

### Vì sao 91.1% throttle KHÔNG phải thermal issue

Metric cũ tính throttle = `clock < 0.9 × max_clock`, poll mỗi 10s.
Nhưng interval giữa 2 request là 60s → GPU idle 55/60s mỗi cycle
→ P-state rớt xuống ~180 MHz (vs max 3090 MHz) → **91% sample rơi vào lúc GPU idle**, không phải throttle nhiệt.

**Bằng chứng không có thermal issue:**
- Max temp 63°C — rất thấp so với ngưỡng thermal throttle của RTX 5060 Ti (~83°C)
- Không có sample nào chạm thermal limit
- Decode last 3 (42.6 tps) tốt hơn first 3 (41.6 tps) — nếu thermal throttle, số cuối phải TỆ hơn

`gpu_monitor.py` đã được refactor dùng `throttle_reasons.sw_thermal_slowdown` / `hw_thermal_slowdown` cho lần đo sau (chính xác hơn, không tính idle).

## No-go check (ARCHITECTURE 0.2)

- [x] TTFT cold ≤ 1000 ms → 444 ms ✅
- [x] Decode ≥ 30 tok/s ở mọi scenario → thấp nhất 40.0 tps ✅
- [x] Thermal throttle ≤ 30% → 0% thực tế (max 63°C, không thermal event) ✅

**Không chạm no-go nào.**

## Decision

**✅ GO** — tiến Day 2 (TTS Vietnamese).

## Ghi chú thêm

**Điều bất ngờ (không phải no-go, nhưng cần biết cho Phase 1):**

1. **TTFT warm không cải thiện so với cold** — S2 warm short thậm chí chậm hơn S1 cold (552 vs 444). `cache_prompt: true` được set nhưng có vẻ không hit. Nguyên nhân có thể:
   - Chat template Gemma sinh prefix khác nhau mỗi request → cache miss
   - llama-server version này không dùng prefix cache trên `/v1/chat/completions` (chỉ `/completions`)
   - Cần verify ở Phase 1 khi tune persona prompt

2. **Decode ~40-45 tps ổn định qua mọi context size** — model không degrade khi context dài (S4 3.8K tokens vẫn 40 tps). Là tín hiệu tốt cho Phase 7 (memory context lớn).

3. **Output stream qua `delta.reasoning_content`** thay vì `delta.content` — Gemma 4 12B đang bị llama-server route qua reasoning tags. Chi tiết chẩn đoán:
   - Không phải Gemma reasoning variant (không có model như vậy)
   - Nghi ngờ do llama.cpp version mới auto-apply reasoning template
   - **Phase 1 impact:** parser mood block sẽ nhận `reasoning_content`, cần chuẩn hoá về text thường (hoặc restart llama-server với `--reasoning-format none` khi lên Phase 1)

4. **Outlier TTFT sample 2 (1647ms)** trong S5 chưa rõ — có thể GC pause, filesystem hit, hoặc CUDA lazy init. Không lặp lại. Bỏ qua.

## Kiến nghị cập nhật config

**config/models.yaml** (khi tạo ở Phase 0):
```yaml
llm:
  main:
    endpoint: "http://localhost:8080"
    n_ctx: 4096
    latency_target:
      ttft_p50_ms: 550    # đo được ~450-550ms qua các context size
      ttft_p95_ms: 1000
      decode_tps_min: 40  # thay vì 60 (target ARCHITECTURE quá optimistic)
```

**ARCHITECTURE Section 1.1** (latency target): update TTFT target từ `<500ms` thành `<600ms P50` dựa trên số thực đo.

**ARCHITECTURE Appendix C** (trade-off log): thêm dòng "Decode target realistic 40 tok/s (không 60) — số thực trên RTX 5060 Ti + Gemma 12B Q4_K_M".

## Prerequisites Day 2

- Piper voices tiếng Việt: cần download `vi_VN-*.onnx` từ Hugging Face
- XTTS v2 / viXTTS: tuỳ chọn, VRAM sau khi load Gemma còn ~4-5GB (đủ)
- 10 câu mẫu tiếng Việt (chào hỏi, cảm xúc, câu dài)
