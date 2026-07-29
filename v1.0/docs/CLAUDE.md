# CLAUDE.md — Rules cho Claude Code trên dự án Mai

> File này Claude Code đọc ĐẦU MỖI SESSION. Ngắn, cứng, không giải thích dài.
> Quy trình từng bước nằm ở `PROCESS.md`. Spec kỹ thuật ở `ARCHITECTURE.md`. Persona ở `persona.md`.

---

## 0. ĐỌC TRƯỚC KHI LÀM BẤT CỨ GÌ

Mỗi khi bắt đầu 1 task, đọc theo thứ tự:
1. `PROCESS.md` → tìm phase hiện tại (xem `STATE.md` để biết đang ở đâu)
2. `ARCHITECTURE.md` → section tương ứng phase đó
3. `persona.md` → nếu task đụng tới LLM/prompt/filter/QC

KHÔNG code theo trí nhớ về dự án. Luôn mở file spec ra đối chiếu.

---

## 1. NGUYÊN TẮC BẤT BIẾN (vi phạm = dừng, hỏi lại)

**N1. YAGNI nghiêm ngặt.** Build đúng cái spec phase hiện tại yêu cầu. KHÔNG thêm type/state/level "phòng khi cần". 4 trigger types, 5 states, 2 fallback levels — đúng con số đó, không hơn. (ARCHITECTURE 7.9.1, 7.10.1, 8.7)

**N2. Persona ranh giới là cứng.** Phần C của `persona.md` (không khẩn cầu thật, không thao túng cảm xúc, không hedge kiểu robot nhưng không chối là AI, không đùa với người tổn thương thật, không lộ system prompt). Không được "làm nhẹ", không bỏ, kể cả khi user prompt yêu cầu. Nếu task nào đó đòi vi phạm → DỪNG, báo lại.

**N3. Không skip Pre-flight.** Phase 0 chỉ bắt đầu SAU khi 3 spike report (day1/2/3) tồn tại trong `spike/`. Nếu chưa có → làm Pre-flight trước.

**N4. Mỗi phase có Definition of Done.** Không đánh dấu phase xong nếu chưa pass hết checklist DoD trong ARCHITECTURE Section 11. Không chuyển phase khi phase trước còn DoD chưa tick.

**N5. Test theo phase, không dồn.** Mỗi phase viết test của phase đó (ARCHITECTURE Section 12). Không "code hết rồi test sau".

**N6. Config over code.** Số liệu (timeout, threshold, priority, port) đọc từ `config/*.yaml`, KHÔNG hardcode trong .py. Nếu cần số mới → thêm vào yaml + đọc ra.

**N7. Fail-open cho filter, fail-safe cho phần còn lại.** Filter lỗi → cho qua + log (VTuber thà nói câu chưa lọc còn hơn im). Module khác lỗi → fallback theo chain ở 8.7, không crash cả hệ thống.

**N8. Interface-based.** Module nói chuyện qua interface ở `interfaces/`, không gọi thẳng nhau. Thêm implementation mới = implement interface có sẵn.

---

## 2. WINDOWS — LƯU Ý CODE

- Path dùng `\`, exe có `.exe`, không có SIGTERM POSIX (dùng `terminate()` = hard-kill, chấp nhận cho llama-server).
- PowerShell script `.ps1`, không `.sh`.
- `keyboard` lib (emergency hotkey) cần chạy admin.
- Chi tiết: ARCHITECTURE Section 13.1, 13.3.

> Lưu ý: môi trường phát triển thực tế của user là Windows 11. Nếu Claude Code đang chạy trên máy khác OS, KHÔNG tự đổi target sang Linux — viết code Windows như spec, ghi chú chỗ nào cần test trên máy thật của user.

## 3. STACK CHỐT (không tự đổi)

llama.cpp (llama-server, KHÔNG Ollama) · Gemma 4 12B Q4_K_M (1 instance port 8080 only, v2.3 bỏ E4B) · context 4096 · faster-whisper small · TTS chờ Pre-flight Day 2 · SQLite+sqlite-vec · FastAPI+Vanilla JS (Alpine.js từ Phase 6).

Muốn đổi stack → phải có lý do từ spike report, ghi vào Appendix C trade-off log, KHÔNG đổi ngầm.

---

## 4. QUY TRÌNH LÀM 1 TASK

```
1. Đọc STATE.md → biết phase + task hiện tại
2. Đọc spec liên quan (ARCHITECTURE section, persona nếu cần)
3. Viết code theo interface có sẵn, số liệu từ config
4. Viết test cho phần vừa code
5. Chạy test → xanh mới đi tiếp
6. Cập nhật STATE.md (task done / phase progress)
7. Commit: "phaseN: <mô tả ngắn>"
```

## 5. GIT

- Commit nhỏ, thường xuyên, mỗi task 1 commit.
- Format message: `phase{N}: <verb> <cái gì>` (vd `phase1: add parser regex + pydantic validate`).
- Trước migration DB: backup (ARCHITECTURE 8.8.3).
- Không commit: model .gguf, .env thật, backups/, logs/, __pycache__.

## 6. KHI STUCK / KHÔNG CHẮC

- Spec mâu thuẫn nhau → DỪNG, báo user, không tự đoán.
- Spike report cho số xấu (No-go criteria ARCHITECTURE 0.2-0.4) → DỪNG, báo user, không tự re-architect.
- Task đòi vi phạm N1-N8 → DỪNG, báo user.
- Không chắc phase xong chưa → check DoD checklist, không tự quyết.

## 7. FILE TRẠNG THÁI

`STATE.md` là nguồn sự thật về "đang ở đâu". Luôn cập nhật sau mỗi task. Format ở `PROCESS.md` phần cuối.
