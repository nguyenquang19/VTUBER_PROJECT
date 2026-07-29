# Spike Day 2 — TTS Vietnamese Quality

Test Piper VN voices trước (0 VRAM). Nếu quality <6/10 mới thử XTTS/viXTTS.
Spec: [ARCHITECTURE.md Section 0.3](../../docs/ARCHITECTURE.md).

## Prerequisites

3 Piper voices đã tải về `models/tts/piper/`:
- `vi_VN-vais1000-medium.onnx` (+.onnx.json) — 22050 Hz, ~63MB, chất lượng cao nhất
- `vi_VN-25hours_single-low.onnx` (+.onnx.json) — 16000 Hz, ~63MB
- `vi_VN-vivos-x_low.onnx` (+.onnx.json) — 16000 Hz, ~28MB

## Chạy

```powershell
cd E:\BAI_CUA_DUC\AI_VTUBER\v1.0
.\venv\Scripts\Activate.ps1
cd spike\day2_tts_quality
python benchmark.py
```

Xuất 30 file WAV vào `samples/<voice>/01_greeting_short.wav` ... `10_mixed_punct.wav`.

**Thời gian:** ~1-2 phút (Piper cực nhanh trên CPU).

## Chấm điểm subjective

Nghe từng file theo thứ tự → chấm vào `day2_report.md` (copy từ `report_template.md`).

**Tiêu chí chấm 1-10:**
- **Naturalness (1-10):** giọng có tự nhiên không, hay robotic
- **Prosody (1-10):** ngữ điệu, nhấn nhá, ngắt câu có đúng không
- **Emotion (0/5/10):** có biểu cảm không (Piper thường 0 vì không có emotion capability)
- **Intelligibility (1-10):** nghe có ra chữ không, có bị nuốt âm không
- **Overall (1-10):** cảm giác tổng quan

Go criteria: ≥1 voice có Overall ≥6/10 + latency <800ms.

## Next step nếu No-go

Nếu Piper max 6/10 mà bạn cần chất lượng cao hơn → tải XTTS v2 / viXTTS:

```bash
# XTTS v2 (đa ngôn ngữ, ~2GB, cần VRAM)
pip install TTS
# viXTTS (VN-tuned, HF: capleaf/viXTTS)
huggingface-cli download capleaf/viXTTS --local-dir models/tts/xtts/vixtts
```

Nhưng chỉ làm sau khi đã confirm Piper không đủ.
