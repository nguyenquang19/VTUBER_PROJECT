# CLAUDE.md

Chỉ dẫn cho Claude Code. **Nguồn canonical là [`AGENTS.md`](AGENTS.md)** — đọc hết file đó trước tiên;
CLAUDE.md không lặp lại nội dung để tránh lệch.

## Tóm tắt tối thiểu

- Working tree: `v2.0/` (repo root). Frozen: `ver/v1.0/` — không sửa. Product version:
  `config/system.yaml::app.version`.
- Đọc theo thứ tự: `AGENTS.md` → `docs/MAI_V2_SYSTEM_SPEC.md` → `docs/V1_BASELINE.md` → `docs/ROADMAP.md`.
- Nguồn sự thật khi mâu thuẫn: `interfaces/` → `orchestrator/stream_runtime.py` → `services/` →
  `config/*.yaml` → `tests/` → SYSTEM_SPEC. Báo conflict trước khi sửa.
- Windows 11/PowerShell, Python 3.11+, `llama.cpp`. Docs-first, một phase mỗi task, targeted test +
  impacted regression, dừng review. Không commit state trước verified success, không cho LLM invent
  capability, không thêm V3.

Mọi chi tiết ràng buộc, invariant, bẫy naming và workflow: xem `AGENTS.md`.
