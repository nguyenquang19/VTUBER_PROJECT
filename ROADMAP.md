# 🗺️ ROADMAP

## ✅ Đã xong — Khung (Skeleton)
- [x] EventBus (pub/sub)
- [x] Orchestrator — turn-taking hai chiều
- [x] Monologue Engine — tự nói có mạch
- [x] Persona 2 tầng (Core + Evolving) + system prompt builder
- [x] Emotion parser
- [x] Mock LLM/TTS + fake chat + test (barge-in, persona)

## 🔜 V1 — Ghép model thật (Discord-only)
- [ ] **Bước A** — LLM Qwen3.5-9B thật qua vLLM (`modules/outputs/llm_vllm.py`)
- [ ] **Bước B** — TTS thật + producer-consumer (`modules/outputs/tts_engine.py`)
- [ ] **Bước C** — Discord bot thật (`modules/inputs/discord_bot.py`)
- [ ] **Bước D** — Avatar VTube Studio (`modules/avatar/vts_controller.py`)
- [ ] **Bước E** — Memory + Reflection (`memory/memory.py`)
- [ ] Stress test 3h + Definition of Done

## 🔮 V2 — Mở rộng
- [ ] STT (nhận giọng nói) + barge-in bằng giọng
- [ ] Vision (nhìn màn hình / chơi game) — dùng multimodal của Qwen3.5
- [ ] Twitch / YouTube ingest
- [ ] DPO-LoRA offline (Tầng 3) — "đóng dấu" giọng đã ổn định
- [ ] Inner Thoughts đầy đủ (Tầng 3 tự nói)
- [ ] Phân tán 3 máy (thêm máy C)

## Nguyên tắc
Mỗi Bước: code → test lẻ xanh → ghép → test tích hợp → mới đi tiếp.
Ít khối, ít máy = ít điểm hỏng. Chỉ mở rộng khi cái trước đã đứng vững.
