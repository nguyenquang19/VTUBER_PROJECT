# Mai — Developer Manual

> Bộ tài liệu phát triển canonical (2026-08). Đọc file này đầu tiên. Các file spec cũ (ARCHITECTURE.md, EMOTION_SIMULATION.md, AUTONOMY_ENGINE_REDESIGN.md, MOOD_SYSTEM.md, phase0_report.md) là REFERENCE HISTORICAL — spec thiết kế, không phải hướng dẫn dùng.

---

## Đọc theo thứ tự

| File | Đọc khi nào |
|---|---|
| **01_architecture.md** | Muốn hiểu tổng thể: layer nào làm gì, data flow 1 turn |
| **02_modules.md** | Cần chi tiết 1 module cụ thể (LLM, TTS, Memory, ...) để sửa/debug |
| **03_operations.md** | Chạy hệ thống, cấu hình, deploy, troubleshoot |
| **04_extending.md** | Thêm module mới hoặc integrate service mới |

---

## Trạng thái codebase (2026-08-06)

- **Test suite:** 927 pass / 3 deselected (LLM live)
- **Phases xong (code + DoD unit):** 0, 1, 2, 3, 4, 7, 7.5, Platform, Autonomy v2
- **Phases chưa làm:** 5 (STT, deferred), 6 (Animation), 8 (QC+Data), 9 (Fine-tune)
- **Live checkpoints treo:** P4/P7/P7.5/Platform/Autonomy — chờ user chạy verify

---

## Doc history & scope

### File spec (design intent, historical — KHÔNG rewrite)
- `docs/ARCHITECTURE.md` — spec kỹ thuật gốc, 13 section
- `docs/PROCESS.md` — quy trình 9 phase
- `docs/persona.md` — persona Mai chi tiết A/B/C
- `docs/EMOTION_SIMULATION.md` — spec Phase 7.5
- `docs/AUTONOMY_ENGINE_REDESIGN.md` — spec Autonomy v2
- `docs/MOOD_SYSTEM.md` — mood v1 (bị 7.5 superseded, giữ historical)
- `docs/phase0_report.md` — báo cáo Phase 0

### File current state (updated per phase)
- `STATE.md` — nguồn sự thật "đang ở đâu"
- `docs/QUICKSTART.md` — 1-page overview
- `docs/SYSTEM_OVERVIEW.md` — snapshot post-Platform (2026-08-05)
- **`docs/dev_manual/*` — file này**

### File Claude rules (không đọc trừ khi làm việc với Claude Code)
- `CLAUDE.md` (2 copy: root + docs/) — rules N1-N8

---

## Convention docs

- **Đường dẫn file:** relative to `v1.0/` (VD `services/llm/llm_turn.py`)
- **Version chốt:** không viết version của thư viện trừ khi có ràng buộc (Python 3.11, torch 2.11+cu128, VieNeu-TTS v3 Turbo — 3 cái này là chốt cứng)
- **Update khi:** phase mới đóng, swap component lớn, hoặc phát hiện sai/lỗi trong doc
