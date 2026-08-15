# Project: Mai V2 — AI VTuber

## Version layout

- `ver/v1.0/` — frozen source snapshot; chỉ dùng baseline, regression, rollback và reference.
- `v2.0/` — current implementation working tree ở repository root.
- Product version hiện tại lấy từ `v2.0/config/system.yaml::app.version`; tên thư mục không tự động
  thay đổi product version.
- Không sửa trực tiếp `ver/v1.0/`.

## Tài liệu bắt buộc

Đọc theo thứ tự:

1. `v2.0/AGENTS.md`.
2. `v2.0/MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md`.
3. `v2.0/docs/00_V1_0_BASELINE.md` và `v2.0/docs/README.md`.
4. Tài liệu module, interface, composition root, implementation, YAML và impacted tests của phase.

Blueprint là source of truth cho scope và thứ tự migration. Code/interfaces/tests/config cùng tài liệu
runtime là source of truth cho behavior đã triển khai. Không coi feature trong blueprint là production
trước khi code, test và release gate tương ứng hoàn tất.

## Ràng buộc bắt buộc

1. Windows 11 và PowerShell; không dùng Bash syntax.
2. LLM backend là llama.cpp (`llama-server.exe`), không dùng Ollama, transformers hoặc vLLM.
3. Python 3.11+, type hints đầy đủ, async/await cho I/O.
4. Service crossing subsystem phải implement interface trong `v2.0/interfaces/`.
5. Feature tùy chọn mới phải đăng ký với `FeatureManager`.
6. Mọi feature mới phải có metric.
7. Threshold/TTL/cooldown/weight production nằm trong YAML, không hardcode.
8. Ưu tiên implementation đơn giản, deterministic và test được.

## Workflow

Trước mỗi phase: đọc → báo files/contracts/tests/risks → docs-first → xác nhận với user → code đúng một
phase. Sau khi code: targeted tests + impacted V1 regression + replay nếu cần → báo metrics/risks → STOP.
Không tự chuyển phase, không xóa V1 fallback trước shadow validation và không thêm logic V3.
