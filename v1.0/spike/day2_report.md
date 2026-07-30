# Spike Day 2 Report — TTS Vietnamese Quality

**Ngày đo:** 2026-07-29 22:00–22:45
**Hardware:** RTX 5060 Ti 16GB (viXTTS chạy GPU, Piper CPU)
**Candidates tested:** Piper (3 voice), viXTTS

## Kết quả subjective (user chấm)

### Piper VN (`vi_VN-vais1000-medium`, `25hours_single-low`, `vivos-x_low`)

- **User verdict:** "voice tts nào cũng dở hết" — tất cả 3 voice REJECT
- **Root cause:** espeak-ng phonemizer sinh ra tone markers VN (2/4/5/6/7/̪) không có trong model id_map → phát âm sai dấu → không thể dùng cho VTuber tiếng Việt

### viXTTS (voice clone từ `vi_sample.wav`)

Iteration:
1. **v1** (`gpt_cond_len=3`, `en` cleaner): "lai tiếng Anh" — accent bị English
2. **v2** (`gpt_cond_len=30`): "đỡ hơn rồi nhưng vẫn có chỗ chưa ổn với đếm tiền sao lại dùng tiếng anh"
3. **v3** (`gpt_cond_len=30` + VN cleaner với `num2words(lang='vi')`): user chốt **GO — chấp nhận baseline, train lại sau**

## Latency

| TTS | avg synth (ms) | avg audio (s) | avg RTF | VRAM |
|---|---|---|---|---|
| Piper vais1000-medium | 158 | 4.00 | 0.040 | 0 |
| Piper 25hours_single-low | 128 | 5.12 | 0.026 | 0 |
| Piper vivos-x_low | 128 | 5.88 | 0.022 | 0 |
| **viXTTS (final v3)** | **2627** | **5.51** | **0.482** | **1.79 GB** |

viXTTS avg 2.6s/câu (vs target <800ms) — không đạt target latency lý tưởng.
Nhưng RTF 0.482 < 1.0 → streaming câu-theo-câu vẫn OK cho Phase 4:
model đọc trước câu N+1 khi Mai đang nói câu N → user không thấy trễ.

### ⭐ Cập nhật 2026-07-30 — TTFA streaming đo thật

Con số 2600ms ở trên là `synthesize()` **blocking cả câu** — dùng để CHẤM chất
lượng, KHÔNG phải metric UX. Đo lại bằng `Xtts.inference_stream()`:

| Câu | Blocking (cả câu) | **Stream TTFA** (âm đầu) | chunks |
|---|---|---|---|
| Câu dài | 2450 ms | **465 ms** | 5 |
| Câu vừa | 1652 ms | **445 ms** | 5 |
| Câu ngắn | 1590 ms | **447 ms** | 4 |

**TTFA ~450ms** → end-to-end (LLM chữ đầu ~0.5s + TTS âm đầu ~0.45s) ≈ **~1s** tới
lúc Mai cất tiếng. ĐẠT target Phase 4 (TTFA <1s).

**QUYẾT ĐỊNH KIẾN TRÚC Phase 4 (bắt buộc):** TTS module dùng `inference_stream()`
(yield chunk), KHÔNG dùng `synthesize()` blocking. `get_conditioning_latents(
gpt_cond_len=30)` gọi 1 lần cache lại, rồi `inference_stream(text,'vi',gpt_lat,
spk,stream_chunk_size=20)`.

## Decision matrix

| TTS | Quality | Latency | VRAM | Emotion | Decision |
|---|---|---|---|---|---|
| Piper vais1000-medium | REJECT | 158ms | 0 | No | **skip** |
| Piper 25hours_single-low | REJECT | 128ms | 0 | No | **skip** |
| Piper vivos-x_low | REJECT | 128ms | 0 | No | **skip** |
| **viXTTS (v3 config)** | Accepted (baseline) | TTFA ~450ms (stream) / 2600ms blocking | 1.79GB | Yes | **primary** |
| Subtitle overlay | N/A | 0 | 0 | N/A | **fallback** (spec 8.7.3) |

## No-go check (ARCHITECTURE 0.3)

- [x] Có ≥1 voice quality ≥6/10 với latency <800ms

Chú thích: viXTTS 2600ms vượt target 800ms nhưng **user chấp nhận** vì:
1. Piper đủ target latency nhưng quality REJECT
2. viXTTS quality user chấp nhận làm baseline
3. RTF 0.482 → streaming per-sentence sẽ giấu latency ở Phase 4

## Decision

**✅ GO** — chốt viXTTS làm primary, tiến Day 3 (STT).

## Config production (cho `config/models.yaml` khi tạo ở Phase 0)

```yaml
tts:
  primary:
    provider: vixtts
    model_dir: models/tts/xtts/vixtts
    reference_audio: models/tts/xtts/vixtts/vi_sample.wav
    params:
      language: vi
      gpt_cond_len: 30       # dùng full 16.5s ref, giọng bám VN hơn
      gpt_cond_chunk_len: 6
      temperature: 0.75
      length_penalty: 1.0
      repetition_penalty: 5.0
    latency_target:
      p50_ms: 2600
      p95_ms: 5000
    vram_gb: 1.79
  fallback:
    provider: subtitle_overlay  # ARCHITECTURE 8.7.3
```

## Known issues cần fix ở phase sau

**Đã note user "train lại sau" — không blocking Day 2:**

1. **English words trong câu VN vẫn phát âm lai** — tên riêng (YouTube, Anthropic, DeepMind, VTuber, ...) được đọc theo phone VN → nghe kỳ. Fix option: pre-process detect English words → hoặc keep as English lang segment, hoặc phiên âm sang VN.

2. **Reference audio `vi_sample.wav` là bản mặc định capleaf** — chưa tối ưu cho persona Mai. Cần thu ref voice riêng cho persona (nữ trẻ, VN Bắc/Nam tùy chọn) rồi replace.

3. **Base viXTTS bias English** — dù v3 đỡ hơn, một số câu vẫn có accent lai. Cần fine-tune thêm với dataset VN thuần cho persona Mai (kết hợp Phase 8 QC + Phase 9 fine-tune).

4. **Setup phụ thuộc runtime patches** (torchcodec bypass + tokenizer patch) — code Phase 4 sẽ package thành `services/tts/vixtts_tts.py` với các patch đóng gói sạch.

## Setup gotchas đã giải quyết

Ghi lại để không lặp lại khi setup ở phase khác / máy khác:

1. **Torch CPU-only vs CUDA** — install lần đầu qua `pip install -r requirements.txt` sẽ pull `torch==2.13.0+cpu`. Cần force cu128:
   ```bash
   pip install --upgrade --force-reinstall torch torchaudio --index-url https://download.pytorch.org/whl/cu128
   ```
2. **transformers >=5.x không tương thích coqui-tts** — cần <5 (đã downgrade về 4.57)
3. **coqui-tts[codec] cần torchcodec** — mà torchcodec 0.15 + FFmpeg 8 = DLL dep mismatch trên Windows → monkey-patch `torchaudio.load` bằng soundfile
4. **Stock coqui-tts không có VN tokenizer** — monkey-patch `VoiceBpeTokenizer.preprocess_text` để accept `lang='vi'` với custom VN cleaner (`num2words(lang='vi')` cho số, whitespace+lowercase)
5. **`gpt_cond_len=3` (default) → giọng lai Anh** — bắt buộc `gpt_cond_len=30` để bám VN accent
6. **English cleaner expand số Anh** — không được dùng English cleaner cho VN text (số 1.250.000 → "one million...")

## Prerequisites Day 3

- faster-whisper `small` — sẽ tải qua `WhisperModel(small)` tự động download lần đầu (~450MB)
- Test set: 20-40 câu tự nói vào mic (nhưng có thể user delegate cho spike ban đầu — dùng dataset chuẩn Common Voice VN, hoặc TTS-generated audio làm proxy)
- Cần verify VRAM sau khi load Gemma + viXTTS + faster-whisper (12B ~ 8GB + viXTTS 1.79GB + Whisper small ~1GB ≈ 11GB / 16GB → còn 5GB buffer, OK)
