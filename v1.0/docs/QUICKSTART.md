# QUICKSTART - Dự án Mai

**Nếu bạn chỉ đọc 1 file, đọc file này.**

---

## Stack tóm tắt (chốt v2.3)

- **OS:** Windows 11 (single target, không dual-support Linux)
- **LLM backend:** llama.cpp (`llama-server.exe`), giao tiếp qua OpenAI-compatible HTTP API
- **Model chính:** Gemma 4 12B Q4_K_M (main), Gemma 4 E4B Q4_K_M (share cho filter + fallback)
- **STT:** faster-whisper small (VI)
- **TTS:** xác định sau Pre-flight Day 2 (Piper / XTTS / viXTTS)
- **Animation:** VTube Studio API
- **Vector store:** SQLite + sqlite-vec (không server riêng)
- **Dashboard:** FastAPI + Vanilla JS (Phase 0-5), thêm Alpine.js từ Phase 6

---

## Bạn nên làm gì trước?

### Nếu chưa bắt đầu gì cả

**Đọc theo thứ tự:**
1. `ARCHITECTURE.md` Section 0 — Pre-flight Validation (4 ngày spike)
2. `ARCHITECTURE.md` Section 1 — Tổng quan mục tiêu
3. `ARCHITECTURE.md` Section 11 — Timeline và Phase breakdown
4. `ARCHITECTURE.md` Section 7.9 — Trigger logic (business core, đổi số từ 7.5 cũ)
5. `ARCHITECTURE.md` Section 13.1 — Windows-specific notes (đọc TRƯỚC khi cài đặt gì)

**Bỏ qua ban đầu, đọc khi cần:**
- Section 4-6: Feature toggle, Metrics, Dashboard (đọc khi build Phase 0)
- Section 7: Interfaces (đọc khi build module)
- Section 8: Module specs (đọc khi build module tương ứng)
- Section 9-10: Data schema, Config (đọc khi cần)

### Nếu đang ở Pre-flight

Chạy 4 ngày spike theo Section 0. **Không skip.**

Kết quả spike quyết định:
- Model choices trong `models.yaml`
- Context size thực tế
- Latency target realistic
- Có phá "100% local" hay không

### Nếu đã pass Pre-flight, chuẩn bị code

Đọc `ARCHITECTURE.md` Section 11 (Phase hiện tại) — có Definition of Done cụ thể.

---

## 3 nguyên tắc bất di bất dịch

**1. Simplicity first.**
Bắt đầu với minimum viable. Add complexity khi thực sự cần, không phải khi nghĩ có thể cần.

**2. Validate before commit.**
Không commit vào assumption chưa test. Spike day rẻ hơn refactor sprint.

**3. Persona is sacred.**
Fine-tune data align với persona core. Có thể tinh chỉnh chi tiết, không được đổi ranh giới đạo đức.

---

## Timeline tổng quan

```
Pre-flight (4 ngày)
   ↓
Phase 0: Infrastructure    (3 tuần)
Phase 1: Core LLM          (3 tuần)
Phase 2: Trigger + State   (2 tuần)
Phase 3: Filter            (2 tuần)
Phase 4: TTS               (4 tuần)
Phase 5: Voice input       (3 tuần)
Phase 6: Animation         (3 tuần)
Phase 7: Memory            (3 tuần)
Phase 8: QC + Data         (3 tuần)
Phase 9: Fine-tune         (2 tuần)
──────────────────────────────────
Total: ~29 tuần (~7 tháng part-time)
```

**Không phải sprint. Không phải deadline cứng. Marathon.**

---

## Nếu bị stuck

**Bug lạ ở Phase X?**
→ Đọc `ARCHITECTURE.md` Section 11 Phase X → Definition of Done + Deliverables list.

**Không biết cách implement feature Y?**
→ Đọc `ARCHITECTURE.md` Section 8 (Module Specs) tương ứng.

**Latency > target?**
→ Đọc `ARCHITECTURE.md` Section 5 (Metrics) để đo. Rồi Section 4.6 (Graceful degradation).

**VRAM > 90%?**
→ Đọc `ARCHITECTURE.md` Section 4.6 (Degradation policy).

**Windows setup lỗi (PowerShell, hotkey, AV block...)?**
→ Đọc `ARCHITECTURE.md` Section 13.1 (Windows-specific notes).

**Muốn rollback change?**
→ Feature toggle qua dashboard. Nếu vẫn broken → git revert.

---

## Cảnh báo cho solo dev

Sau khi review nhiều lần, mình rút ra 5 warning quan trọng nhất:

**1. Không đọc hết doc trước khi code.**
Đọc Section 0 + 1 + 11 + 13.1 (Windows notes), rồi bắt tay Pre-flight. Đọc thêm khi cần.

**2. Đừng skip Pre-flight vì "sẽ làm sau".**
Nhiều dev skip → 4 tuần sau phát hiện TTS Vietnamese không đủ tốt → refactor toàn bộ. Đã cảnh báo.

**3. Đừng build 12+ trigger types trong Phase 2.**
Chỉ 4 types đầu (operator_voice/chat_mention/chat_normal/ambient_talk). Thêm type mới khi log thực tế cho thấy cần. YAGNI.

**4. Đừng tune persona quá sớm.**
Chạy prompt v1 qua 100 turn trước khi tune. Đa số vấn đề tự resolve khi bạn hiểu Mai thực tế trả lời thế nào.

**5. Đừng fine-tune vội.**
Phase 9 (fine-tune) là **cuối cùng**, không phải giải pháp cho persona drift ở Phase 1. Prompt engineering trước, fine-tune sau.

---

## Ranh giới cần giữ vững

Đây là những thứ **không được thoả hiệp** dù áp lực delivery:

- Persona ranh giới đạo đức (chi tiết trong `persona.md` — không thao túng cảm xúc, không khẩn cầu, không hedging kiểu robot)
- Emergency stop luôn hoạt động (Ctrl+Shift+X, cần Admin quyền để hook global)
- Filter fail-open OK, filter fail silent KHÔNG OK
- Content trẻ em, harmful → luôn block, không có toggle
- Không cào data từ VTuber khác (đã bàn kỹ trong conversation)

---

## Cần help?

Trong process code:
- Bug nhỏ → tra `ARCHITECTURE.md` Section 11 phase hiện tại
- Bug lớn → check `ARCHITECTURE.md` Section 13 (Operations)
- Kiến trúc question → `ARCHITECTURE.md` Appendix C (Trade-off log)
- Persona question → `persona.md` (không phải doc này)

---

**Sẵn sàng chưa? Bắt đầu Pre-flight ngay.**

Xem `ARCHITECTURE.md` Section 0.
