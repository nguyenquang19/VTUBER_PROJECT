# MOOD SYSTEM — tổng quan (index)

> File này nối 3 nguồn về mood để khỏi lạc: **format** (persona.md Phần B), **engine v2**
> (EMOTION_SIMULATION.md), và **kiểu dữ liệu dùng chung** (`interfaces/animation.py`).
> KHÔNG lặp lại nội dung — chỉ trỏ đúng chỗ.

---

## 1. Năm chiều cảm xúc (bất biến)

`vui · buon · buc · bon_chon · nguong` — thang **0-10**, key KHÔNG dấu.
Nguồn sự thật kiểu dữ liệu: `MoodState` trong `interfaces/animation.py` (Pydantic, `dominant()`).
N1: đúng 5 chiều. Đổi schema mood = migrate + train lại từ base (persona.md Phần B ghi chú).

## 2. Hai đời hệ thống mood

| | v1 — Self-report (Phase 1, ĐÃ CÓ) | v2 — Appraisal Engine (Phase 7.5, KẾ HOẠCH) |
|---|---|---|
| Nguồn mood | LLM tự xuất mood block sau câu | Appraisal rule-based (20 category + 4 timer + 3 modifier) |
| Vai LLM mood block | Nguồn chính | **Kênh B** (nudge nhẹ + input QC drift) |
| Format output | `[câu]` + `[vui:N...]` + lý do + còn nữa | **Giữ y nguyên** |
| Cơ chế | snapshot rời rạc | spring-damper, quán tính, tự về baseline |
| Spec | `persona.md` Phần B | `docs/EMOTION_SIMULATION.md` |

**Quan trọng:** chuyển v1→v2 **không đổi format output** đã code ở Phase 1. Chỉ thêm 3 tầng
trước LLM (phân loại → appraisal → MoodEngine) và đảo vai mood block.

## 3. Bản đồ khái niệm → code

| Khái niệm | Ở đâu | Trạng thái |
|---|---|---|
| Kiểu `MoodState` (5 chiều) | `interfaces/animation.py` | ✅ có |
| Format mood block + parse | `services/llm/parser.py` (`ParsedResponse.mood`) | ✅ có |
| Persona + format bắt buộc | `config/prompts/persona_system.txt`, `persona.md` Phần B | ✅ có |
| Appraisal category + target | EMOTION_SIMULATION.md Mục 3-4 | ⏳ Phase 7.5 |
| `MoodEngine` spring-damper | `orchestrator/mood_engine.py` + `config/mood_engine.yaml` | ⏳ Phase 7.5 |
| Cờ tone `force_gentle_tone`/`force_deflect` | Prompt + Filter (Phase 3) | ⏳ Phase 7.5 |
| Drift detector 2 kênh | `services/qc/drift_detector.py` | ⏳ Phase 7.5 |

## 4. Đối chiếu tương thích

Xem `docs/EMOTION_SIMULATION.md` Mục 10 (Đối chiếu codebase P0-2): thiết kế v2 là **thêm mới**,
KHÔNG sửa code Phase 0-2. Điểm khớp nối chính: `MoodState` 5 chiều đã trùng `baseline` của MoodEngine.
