# Tài liệu kỹ thuật Mai

Bộ tài liệu này được tổ chức theo nhiệm vụ bảo trì, không theo lịch sử milestone.

| Tài liệu | Dùng khi nào |
|---|---|
| [01 — Tổng quan](01_SYSTEM_OVERVIEW.md) | Hiểu phạm vi, kiến trúc, process và dependency chính |
| [02 — Pipeline dữ liệu](02_DATA_PIPELINE.md) | Theo dấu input → decision → LLM → delivery → commit |
| [03 — Component reference](03_COMPONENT_REFERENCE.md) | Xác định class/file chịu trách nhiệm khi có bug |
| [04 — Data và storage](04_DATA_AND_STORAGE.md) | Tra contract, JSONL, SQLite, snapshot, backup |
| [05 — Config và feature](05_CONFIGURATION.md) | Tune hành vi, bật/tắt feature, kiểm tra dependency |
| [06 — Operations và troubleshooting](06_OPERATIONS_AND_TROUBLESHOOTING.md) | Live, health, dashboard, incident, sửa lỗi theo triệu chứng |
| [07 — Testing và extension](07_TESTING_AND_EXTENSION.md) | Thêm adapter/feature/action mà không phá boundary |
| [08 — Security và recovery](08_SECURITY_RECOVERY.md) | PII, credential, emergency, rollback, restore |

## Quy ước tài liệu

- Đường dẫn là tương đối từ repository root trừ khi ghi rõ khác.
- `Input`/`Output` là dữ liệu qua boundary, không phải biến nội bộ.
- `Commit` nghĩa là side effect nghiệp vụ được phép ghi sau delivery thành công.
- `Degraded success` nghĩa là audio lỗi nhưng toàn bộ nội dung đã tới subtitle sink.
- `Fail-safe` nghĩa là subsystem lỗi nhưng runtime chính tiếp tục; không đồng nghĩa dữ liệu được commit.
- File trong `docs/baselines/*.json` là evidence máy đọc, không phải hướng dẫn chỉnh tay.

## Bản đồ sửa lỗi nhanh

| Triệu chứng | Đọc trước | File code đầu tiên |
|---|---|---|
| Không nhận chat | 02, 06 | `services/input/chat_router.py` và adapter platform |
| Chọn sai chat/hành động | 02, 03 | `services/director/director.py` |
| Câu trả lời sai context | 02, 03, 04 | `services/llm/prompt_manager.py`, `services/llm/llm_turn.py` |
| Mood/giọng không tự nhiên | 03, 05 | `services/emotion/hybrid_affect.py`, `config/affect_v2.yaml` |
| Có text nhưng không audio | 02, 06 | `services/tts/tts_pipeline.py` |
| Đã nói nhưng history/goal không đổi | 02, 04 | transaction + `finalize_delivery()` |
| History/goal đổi dù không nói | 02, 04 | `services/director/action_transaction.py` |
| Dashboard báo sai | 03, 06 | `dashboard/dashboard_server.py` |
| Log/DB tăng hoặc sai dữ liệu | 04, 08 | logger, memory/relationship store |
