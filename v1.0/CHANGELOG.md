# Changelog — Mai

Mọi thay đổi product sau baseline dùng Semantic Versioning. Product version nằm tại
`config/system.yaml::app.version`; schema/component version là trục độc lập.

## [1.0.1] — 2026-08-12

### Changed
- Chuẩn hóa tên file test: bỏ hậu tố phase/milestone (`_m3`…`_m10`, `phase1`, `phase2`, `m8_...`), đặt tên
  theo component/hành vi. Không đổi nội dung/logic test; regression offline vẫn xanh.
- Ghi rõ nguyên tắc **docs-first** và quy ước đặt tên/runner test trong `AGENTS.md`,
  `docs/07_TESTING_AND_EXTENSION.md` và `CLAUDE.md` (thư mục cha).

### Removed
- Bỏ `scripts/test_phases.py` (runner ad-hoc gom theo phase); dùng `pytest` + marker và các lệnh theo
  vùng trong `docs/07` §10.

### Fixed
- Viết lại `CLAUDE.md` ở thư mục cha thành tài liệu markdown trỏ đúng vào `v1.0/` (trước đây là lệnh
  `Out-File` bị lưu nhầm và trỏ tới `QUICKSTART.md`/`ARCHITECTURE.md`/`PHASE.md` không tồn tại).

Không thay đổi runtime, interface, CLI, storage hay data contract. Rollback: revert commit patch này.

## [1.0.0] — 2026-08-12

Baseline đầu tiên được đóng dấu cho toàn bộ runtime Mai:

- YouTube/Discord text input và llama.cpp generation;
- Director, salience/chat gate, transaction/idempotency và delivery-aware commit;
- Mood Hybrid, Thought Engine self-talk và Conversation Thread Engine;
- VieNeu-TTS, subtitle-only degraded mode và OBS subtitle sink;
- working/semantic/relationship memory theo feature/runtime flag;
- operator dashboard, health/recovery/emergency/shutdown;
- deterministic replay, real llama.cpp stress, evaluation/release tooling;
- versioned raw/canonical/dataset pipeline, backup/restore và privacy boundary.

Contract baseline: architecture `mai-agent-v1`, turn schema `3`, delivery outcome schema `1`, canonical
schema `1`, SFT/DPO schema `1`.

Chi tiết và giới hạn production nằm trong `docs/00_V1_0_BASELINE.md`.

## Quy tắc entry tiếp theo

- Không thêm mục vào `1.0.0` để hợp thức hóa feature phát sinh sau baseline.
- Tạo heading version mới, nêu Added/Changed/Fixed/Removed và migration/rollback nếu có.
- Breaking contract phải ghi rõ dữ liệu/version nào còn tương thích.
