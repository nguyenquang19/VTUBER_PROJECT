# Mai — Developer Manual (canonical)

> Bộ tài liệu kỹ thuật DUY NHẤT của dự án. Đọc file này đầu tiên. Phản ánh code THẬT
> tại 2026-08-06 (sau A1 + C0 Director). Nếu doc lệch code → code đúng, sửa doc.

## Đọc theo thứ tự

| File | Đọc khi nào |
|---|---|
| **01_architecture.md** | Hiểu tổng thể: layer, data flow 1 turn (chat + tự nói), driver Director |
| **02_modules.md** | Chi tiết 1 module (LLM/TTS/Memory/Emotion/Autonomy/**Director**/Platform) để sửa/debug |
| **03_operations.md** | Chạy hệ thống, config, dashboard (tab Mood), test, troubleshoot |
| **04_extending.md** | Thêm module/nguồn mới, hiểu 2 đường điều phối |

## Tài liệu khác trong `docs/` (không phải dev-manual)

| File | Vai trò |
|---|---|
| `CLAUDE.md` | Rules N1-N8 cho AI làm việc với repo (đọc trước khi sửa code) |
| `persona.md` | Persona Mai chi tiết A/B/C (nội dung nhân vật, không phải kỹ thuật) |
| `ROADMAP_AUTONOMOUS_HOST.md` | Kế hoạch tiến tới "tự điều hành 1 buổi stream" |
| `FIX_PLAN_C0_AUDIT.md` | Plan vá lỗi audit C0 (Task 1-7 chờ làm, Task 8 xong) |
| `baselines/` | Baseline transcript/metric để so trước–sau cải tiến |

## Trạng thái codebase (2026-08-06)

- **Phases xong (code):** 0, 1, 2, 3, 4, 7, 7.5, Platform, Autonomy v2, **A1 (de-AI), C0 (Director)**
- **Chưa làm:** 5 (STT, deferred), 6 (Avatar), 8 (QC+Data), 9 (Fine-tune)
- **Đổi lớn gần đây:**
  - **A1**: bỏ mood block khỏi output LLM; mood engine (appraisal) là ground-truth duy nhất;
    Kênh B + DriftDetector tắt; persona thêm few-shot register.
  - **C0**: `services/director/` (SaliencePool + ChatPulse + Director + DirectorLoop) thay
    FIFO. ChatRouter chuyển intake mode. Dashboard thêm tab Mood.
- **Nợ kỹ thuật (xem FIX_PLAN_C0_AUDIT.md):** superchat ack thiếu segment, ack bằng ID,
  SUMMARY lặp, ChatPulse chưa nối mood, v.v.

## Convention

- **Đường dẫn:** relative to `v1.0/` (VD `services/director/director.py`).
- **Chốt cứng:** Python 3.11, torch 2.11+cu128, Gemma 4 12B Q4, VieNeu-TTS v3 Turbo.
- **2 đường điều phối:** stream = Director (§12 của `02_modules.md`). `main.py`/`cli.py`-không-director
  dùng TriggerManager/TurnOrchestrator/StateMachine (legacy). Xem `04_extending.md`.
- **Update doc khi:** đổi logic module, thêm/bỏ thành phần, hoặc phát hiện doc sai.

## Lịch sử doc

Các spec thiết kế cũ (ARCHITECTURE, EMOTION_SIMULATION, AUTONOMY_ENGINE_REDESIGN, MOOD_SYSTEM,
PROCESS, QUICKSTART, SYSTEM_OVERVIEW, phase0_report) **đã bị xoá 2026-08-06** khi chuẩn hoá về
bộ dev_manual này. Nội dung đúng đã gộp vào 01–04. Dev_manual là nguồn duy nhất.
