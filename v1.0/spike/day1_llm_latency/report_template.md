# Spike Day 1 Report — LLM Latency

**Ngày đo:** YYYY-MM-DD
**Hardware:** RTX 5060 Ti 16GB, <CPU>, <RAM>
**Model:** gemma-4-12b-Q4_K_M.gguf
**llama.cpp version:** <commit hoặc release>
**Flags:** `-ngl 999 -c 4096 --cache-type-k q8_0 --cache-type-v q8_0`

## Kết quả đo

| Scenario | Context (prompt_tok) | TTFT (ms) | Decode (tok/s) | Target TTFT | Target decode | Pass? |
|---|---|---|---|---|---|---|
| S1 Cold start | ~500 | <FILL> | <FILL> | <500 | >50 | ✅/❌ |
| S2 Warm short | ~500 (cached) | <FILL> | <FILL> | <300 | >60 | ✅/❌ |
| S3 Warm medium | ~2000 | <FILL> | <FILL> | <800 | >45 | ✅/❌ |
| S4 Warm long | ~4000 | <FILL> | <FILL> | <1500 | >35 | ✅/❌ |

## S5 Overheating (30 phút, 2K prompt liên tục)

| Metric | Giá trị | Target | Pass? |
|---|---|---|---|
| TTFT first 3 avg | <FILL> ms | — | — |
| TTFT last 3 avg | <FILL> ms | <1000 | ✅/❌ |
| Decode first 3 avg | <FILL> tok/s | — | — |
| Decode last 3 avg | <FILL> tok/s | >40 | ✅/❌ |
| GPU max temp | <FILL> °C | <83°C | ✅/❌ |
| Throttle ratio | <FILL> % | <30% | ✅/❌ |

## No-go check (ARCHITECTURE 0.2)

- [ ] TTFT cold ≤ 1000 ms
- [ ] Decode ≥ 30 tok/s ở mọi scenario
- [ ] Throttle ratio ≤ 30%

## Decision

**[ ] GO** — tiến Day 2 (TTS Vietnamese)
**[ ] NO-GO** — dừng, cần user quyết định:
- Reason: <FILL>
- Options: <FILL — theo Section 0.2>

## Ghi chú thêm

- Notes về hardware behavior (fan, thermal, VRAM peak):
- Bất thường quan sát được:
- Kiến nghị cập nhật config/models.yaml:
