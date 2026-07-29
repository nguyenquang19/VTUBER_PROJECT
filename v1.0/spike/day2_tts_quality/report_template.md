# Spike Day 2 Report — TTS Vietnamese Quality

**Ngày đo:** YYYY-MM-DD
**Voices tested:** vais1000-medium, 25hours_single-low, vivos-x_low
**Hardware:** CPU only (Piper), no VRAM used

## Latency

| Voice | Sample rate | Avg synth (ms) | Avg audio (s) | Avg RTF |
|---|---|---|---|---|
| vais1000-medium | 22050 Hz | <FILL> | <FILL> | <FILL> |
| 25hours_single-low | 16000 Hz | <FILL> | <FILL> | <FILL> |
| vivos-x_low | 16000 Hz | <FILL> | <FILL> | <FILL> |

RTF < 1.0 = synth nhanh hơn realtime (tốt cho streaming).

## Quality (1-10 subjective — nghe file trong samples/<voice>/)

### vais1000-medium

| # | Sample | Naturalness | Prosody | Emotion | Intelligibility | Overall |
|---|---|---|---|---|---|---|
| 01 | greeting_short | | | | | |
| 02 | emotion_happy | | | | | |
| 03 | emotion_sad | | | | | |
| 04 | question | | | | | |
| 05 | long_sentence | | | | | |
| 06 | numbers_money | | | | | |
| 07 | names_english | | | | | |
| 08 | exclamation | | | | | |
| 09 | ellipsis_pause | | | | | |
| 10 | mixed_punct | | | | | |
| **AVG** | | | | | | |

### 25hours_single-low

| # | Sample | Naturalness | Prosody | Emotion | Intelligibility | Overall |
|---|---|---|---|---|---|---|
| 01 | greeting_short | | | | | |
| 02 | emotion_happy | | | | | |
| 03 | emotion_sad | | | | | |
| 04 | question | | | | | |
| 05 | long_sentence | | | | | |
| 06 | numbers_money | | | | | |
| 07 | names_english | | | | | |
| 08 | exclamation | | | | | |
| 09 | ellipsis_pause | | | | | |
| 10 | mixed_punct | | | | | |
| **AVG** | | | | | | |

### vivos-x_low

| # | Sample | Naturalness | Prosody | Emotion | Intelligibility | Overall |
|---|---|---|---|---|---|---|
| 01 | greeting_short | | | | | |
| ... | ... | | | | | |
| **AVG** | | | | | | |

## Decision matrix

| TTS | Quality avg | Latency (avg synth) | VRAM | Emotion | Decision |
|---|---|---|---|---|---|
| Piper vais1000-medium | <FILL> | <FILL> | 0 | No | primary / fallback / skip |
| Piper 25hours_single-low | <FILL> | <FILL> | 0 | No | |
| Piper vivos-x_low | <FILL> | <FILL> | 0 | No | |
| XTTS v2 (nếu Piper<6) | | | ~2GB | Yes | |
| viXTTS (nếu Piper<6) | | | ~2GB | Yes | |

## No-go check (ARCHITECTURE 0.3)

- [ ] Có ≥1 voice quality ≥6/10 với latency <800ms

## Decision

**[ ] GO** — chọn primary + fallback → tiến Day 3 STT
**[ ] TIẾP TỤC** — Piper không đủ, cần test XTTS/viXTTS
**[ ] NO-GO** — tất cả <6/10 → cân nhắc:
- Chấp nhận Piper v1, upgrade sau
- Phá "100% local", dùng edge-tts
- Fine-tune TTS

## Ghi chú

**Chosen primary:** <FILL>
**Fallback:** subtitle overlay (ARCHITECTURE 8.7.3) hoặc voice thứ 2
**Note về câu problematic:**
- Câu nào Piper vấp: <FILL>
- Vấn đề tokenization VN (dấu, âm cuối): <FILL>
