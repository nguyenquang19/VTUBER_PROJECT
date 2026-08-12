# Changelog — Mai

Mọi thay đổi product sau baseline dùng Semantic Versioning. Product version nằm tại
`config/system.yaml::app.version`; schema/component version là trục độc lập.

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
