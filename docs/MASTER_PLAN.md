# 🗺️ TOÀN BỘ GIAI ĐOẠN DỰ ÁN — AI VTUBER (Miu)

> File tổng hợp DUY NHẤT — mọi quyết định đã chốt trong suốt quá trình thiết kế.
> Đánh dấu ✅ khi xong. Đi ĐÚNG THỨ TỰ, không nhảy cóc.

---

## 📌 CÁC QUYẾT ĐỊNH ĐÃ CHỐT (tra cứu nhanh)

| Hạng mục | Đã chốt |
|---|---|
| Phạm vi V1 | Discord-only. KHÔNG STT, KHÔNG game/vision, KHÔNG Twitch/YouTube |
| LLM | Qwen3.5-9B, bản **uncensored + nothink** (`nandukmelath/...-nothink-GGUF`) |
| Định dạng model | **GGUF + llama.cpp** (không dùng AWQ/vLLM — convert nhẹ hơn cho train sau) |
| Nền tảng chạy | **WSL2 (Ubuntu)** trên máy A, Windows thuần trên máy B |
| Persona | 2 tầng: Core (bất biến) + Evolving (AI cập nhật qua reflection) |
| Tính cách | Lầy có duyên + dễ thương, có ranh giới an toàn trong Core |
| Tự nói | 3 tầng: Filler + Monologue Engine (trọng tâm V1) + turn-taking luật (Tầng 3 rút gọn) |
| Học liên tục | 3 tầng: Tầng 1 (context) + Tầng 2 (memory/reflection — **chính**) + Tầng 3 (DPO-LoRA, hàng tháng) |
| Fine-tune | **DPO-LoRA** (không phải SFT đơn thuần) — học từ preference của khán giả |
| Máy | A=5060Ti 16GB (não), B=3050 (sân khấu), C=3050 6GB WiFi (để dành V2) |
| Hạ tầng code | EventBus asyncio, Orchestrator turn-taking 2 chiều, Monologue Engine — ĐÃ XONG + test PASS |
| Repo | GitHub `nguyenquang19/VTUBER_PROJECT`, có CI, docker-compose (Qdrant+Redis) |

---

## GIAI ĐOẠN 0 — Hạ tầng & Repo ✅ ĐÃ XONG
- [x] Cấu trúc thư mục chuẩn (core/modules/config/tests/docs)
- [x] `.gitignore`, `LICENSE`, `requirements.txt`
- [x] EventBus (pub/sub, asyncio)
- [x] Orchestrator (turn-taking 2 chiều, barge-in, về mạch)
- [x] Monologue Engine (tự nói có mạch, chống lặp/lạc đề)
- [x] Persona Core + Evolving (yaml) + system prompt builder
- [x] Emotion parser (`[emotion:X]`)
- [x] Mock LLM/TTS + fake chat — khung chạy được, test PASS (11 test)
- [x] GitHub Actions CI + docker-compose (Qdrant, Redis)
- [x] Push lên GitHub

---

## GIAI ĐOẠN 1 — Dựng môi trường model (ĐANG LÀM)
> Không phải code — là cài đặt. Làm xong mới sang Giai đoạn 2.

- [ ] **1.1** Kiểm tra `nvidia-smi` chạy được trong WSL2
- [ ] **1.2** Build `llama.cpp` với CUDA (`docs/setup_llamacpp.md` Bước 1)
- [ ] **1.3** Tải model GGUF uncensored-nothink (~5.63GB)
- [ ] **1.4** Chạy `llama-server` ở port 8000
- [ ] **1.5** ✅ Checkpoint: `curl` ra câu trả lời tiếng Việt

📄 Hướng dẫn đầy đủ: `docs/setup_llamacpp.md`

---

## GIAI ĐOẠN 2 — Bước A: Ghép LLM thật vào khung
- [ ] **2.1** Viết đầy đủ `modules/outputs/llm_client.py`:
  - Gọi llama-server qua client OpenAI-compatible, `stream=True`
  - Sentence-streaming: gom token → yield từng CÂU (không đợi cả đoạn)
  - Strip `<think>...</think>` (phòng hờ dù server đã tắt)
  - Nạp system prompt từ `persona.build_system_prompt()`
- [ ] **2.2** Thay `MockLLM` → `LLMClient` trong `main.py`
- [ ] **2.3** Test lại `fake_chat` scenario — nghe/đọc câu THẬT do model sinh
- [ ] **2.4** Đo độ trễ time-to-first-sentence (mục tiêu tham khảo: <500ms lý tưởng, <800ms chấp nhận được)
- [ ] **2.5** Kiểm tra persona giữ giọng đúng qua 20+ lượt liên tục
- ✅ Checkpoint: Miu trả lời + tự nói bằng câu THẬT, đúng giọng lầy-dễ-thương

---

## GIAI ĐOẠN 3 — Bước B: TTS thật (giọng nói)
- [ ] **3.1** Chọn & cài TTS (GPT-SoVITS clone giọng, hoặc Kokoro nhẹ — quyết theo nhu cầu)
- [ ] **3.2** Viết đầy đủ `modules/outputs/tts_engine.py`:
  - Producer-consumer đa luồng (tổng hợp nền, phát tuần tự)
  - Text normalization (bỏ `[emotion:X]`, số→chữ, bỏ emoji)
  - `stop()` cắt được giữa chừng (cho barge-in)
- [ ] **3.3** Warm-up model trước khi dùng (giảm trễ câu đầu)
- [ ] **3.4** Test: 10 câu liên tiếp không đứt/không chồng tiếng
- [ ] **3.5** Đo first-packet latency (mục tiêu <200ms)
- ✅ Checkpoint: nghe được giọng Miu thật, mượt, không đứt quãng

---

## GIAI ĐOẠN 4 — Bước C: Discord thật
- [ ] **4.1** Tạo bot tại Discord Developer Portal
- [ ] **4.2** ⚠️ BẬT **Message Content Intent** (lỗi hay quên nhất)
- [ ] **4.3** Viết đầy đủ `modules/inputs/discord_bot.py`
- [ ] **4.4** Token vào `.env` (KHÔNG commit)
- [ ] **4.5** Thay `fake_chat` → `DiscordInput` trong `main.py`
- [ ] **4.6** Test: gõ chat Discord thật → Miu trả lời bằng giọng
- ✅ Checkpoint: gõ Discord → nghe Miu nói, <600ms tới tiếng đầu

---

## GIAI ĐOẠN 5 — Bước D: Avatar (VTube Studio, máy B)
- [ ] **5.1** Cài VTube Studio trên máy B, bật API port 8001
- [ ] **5.2** Đặt hotkey biểu cảm khớp danh sách trong `core/emotion.py`
  (happy, laugh, pout, surprised, smug, shy, sad, thinking)
- [ ] **5.3** Viết đầy đủ `modules/avatar/vts_controller.py` (pyvts)
- [ ] **5.4** Lip-sync: route audio TTS → VTS (cách đơn giản trước)
- [ ] **5.5** Watchdog: VTS treo → tự reconnect, avatar về idle
- [ ] **5.6** Cài OBS, dựng cảnh, encode NVENC
- ✅ Checkpoint: avatar nhép miệng khớp tiếng + đổi biểu cảm đúng lúc

---

## GIAI ĐOẠN 6 — Bước E: Memory + Reflection (biết lớn lên)
- [ ] **6.1** `docker compose up -d` (Qdrant + Redis đã có sẵn trong repo)
- [ ] **6.2** Cài embedding đa ngôn ngữ (bge-m3) cho tiếng Việt
- [ ] **6.3** Viết đầy đủ `memory/memory.py`:
  - `MemoryStore`: ADD/UPDATE/DELETE, temporal boosting, forgetting
  - `Reflection`: sau mỗi buổi → tóm tắt + rút lesson
- [ ] **6.4** Nối `Reflection` → `monologue.add_topic()` (chuyện mới để tự nói)
- [ ] **6.5** Nối `Reflection` → cập nhật `persona_evolving.yaml` (KHÔNG đụng Core)
- [ ] **6.6** Digest hàng tuần: xem AI "đổi gì", sửa tay nếu lệch hướng
- ✅ Checkpoint: sau 1 buổi test, Miu nhớ tên người vừa chat + có lesson mới

---

## GIAI ĐOẠN 7 — Tích hợp & Stress Test (Definition of Done V1)
- [ ] **7.1** IT-6: chạy liên tục 3 giờ, chat dồn dập, để lặng dài
- [ ] **7.2** Ngắt WiFi/restart VTS giữa chừng → hệ thống tự phục hồi
- [ ] **7.3** Kiểm tra không rò rỉ VRAM/RAM qua thời gian dài
- [ ] **7.4** Toàn bộ 8 tiêu chí "Definition of Done" (xem `ai-vtuber-v1.md` mục 6)
- ✅ **V1 HOÀN THÀNH khi tất cả tiêu chí trên đạt**

---

## GIAI ĐOẠN 8 — Tầng 3: DPO-LoRA (vài tháng sau, KHÔNG vội)
> Chỉ làm khi V1 đã chạy ổn định nhiều tuần.

- [ ] **8.1** Thu thập cặp preference (chosen/rejected) từ tín hiệu chat thực tế
- [ ] **8.2** Tải bản gốc `HauhauCS/Qwen3.5-9B-Uncensored-...-Aggressive` (~18GB, CHỈ lúc này)
- [ ] **8.3** QLoRA + DPO bằng Unsloth (offline, không phải lúc live)
- [ ] **8.4** Eval gate: test persona + an toàn + năng lực cũ — không đạt thì loại
- [ ] **8.5** Convert bản đã train → GGUF mới (CPU+RAM, không cần 20GB+ VRAM)
- [ ] **8.6** Version + rollback — thay GGUF cũ bằng GGUF mới nếu đạt
- ✅ Checkpoint: model "đóng dấu" được chất giọng đã ổn định qua Tầng 2

---

## GIAI ĐOẠN 9 — V2: Mở rộng (sau khi V1 vững)
- [ ] STT (nhận giọng nói) + barge-in bằng giọng thật
- [ ] Vision (nhìn màn hình/chơi game) — dùng khả năng multimodal của Qwen3.5
- [ ] Twitch / YouTube ingest
- [ ] Inner Thoughts đầy đủ (Tầng 3 tự nói — thay luật đơn giản hiện tại)
- [ ] Phân tán 3 máy thật (kích hoạt máy C cho vision/TTS phụ)
- [ ] (Tuỳ chọn) So sánh lại AWQ+vLLM vs GGUF+llama.cpp bằng đo thực tế

---

## 🧭 NGUYÊN TẮC XUYÊN SUỐT (nhắc lại khi phân vân)
1. **Làm hẹp, làm chắc** — mỗi Bước: code → test lẻ xanh → ghép → test tích hợp.
2. **Mock trước, thật sau** — không code kèm test đồ thật khi logic chưa chắc.
3. **AI luôn có mạch tự nói làm nền** — chat chen vào rồi về mạch, không phải chatbot chờ hỏi.
4. **Core persona bất biến** — chỉ Evolving được AI tự cập nhật.
5. **"Lớn lên" = Tầng 2 (memory/reflection) là chính** — Tầng 3 (đổi trọng số) hiếm, cuối cùng, có eval gate.
6. **Ít máy, ít khối = ít điểm hỏng** — chỉ mở rộng khi cái trước đã đứng vững.
