# Project: Mai V2 — AI VTuber

> Current working tree: root `v2.0/`.
>
> Frozen source snapshot: `ver/v1.0/` — không sửa trực tiếp.
>
> Product version source: `v2.0/config/system.yaml::app.version`.

## Điểm vào bắt buộc

- `AGENTS.md` — version layout và workflow cấp repo.
- `v2.0/AGENTS.md` — ràng buộc trong working tree.
- `v2.0/MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md` — scope, phase order và release gates.
- `v2.0/docs/00_V1_0_BASELINE.md` — frozen baseline/invariants.
- `v2.0/docs/README.md` — index tài liệu runtime.

Không tìm `PHASE.md`, `docs/QUICKSTART.md` hoặc `docs/ARCHITECTURE.md`. Blueprint V2 là execution plan
canonical; các tài liệu `00`–`08` mô tả behavior đã triển khai và phải được cập nhật theo từng phase.

## Source-of-truth order

Cho behavior hiện hành: interfaces/models → `orchestrator/stream_runtime.py` → `services/` → YAML trong
`config/` → tài liệu runtime. Cho scope/thứ tự migration: blueprint V2. Nếu có conflict, báo trước khi sửa;
không tự đoán và không rewrite lịch sử V1.

## Quy tắc làm việc

- Windows 11/PowerShell, Python 3.11+, llama.cpp.
- Docs-first trước thay đổi code/config/contract/structure.
- Interface-based, feature-flagged, observable, config-over-code và fail-safe.
- Làm đúng một phase mỗi task; targeted tests + impacted V1 regression; dừng để user review.
- Không commit state trước verified success, không cho LLM invent capability, không thêm V3 scope.
