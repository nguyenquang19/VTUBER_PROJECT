# Project: Mai — AI VTuber

> **Product hiện tại:** Mai **v1.0.0** — frozen baseline (2026-08-12).
> **Source of truth version:** `v1.0/config/system.yaml::app.version`.
> Code sản phẩm nằm trong `v1.0/`. Đây **KHÔNG** phải phiên bản deprecated.

## Tài liệu chính (đọc trước khi làm bất cứ việc gì)

- `v1.0/AGENTS.md` — điểm vào workflow, ràng buộc, thứ tự source of truth.
- `v1.0/docs/00_V1_0_BASELINE.md` — baseline, capability production/optional/interface-only, invariant, version policy.
- `v1.0/docs/README.md` — mục lục tài liệu chuẩn hóa; chọn tài liệu theo loại task.
- `v1.0/docs/01_SYSTEM_OVERVIEW.md` … `08_SECURITY_RECOVERY.md` — chi tiết từng vùng.

**KHÔNG** có `docs/QUICKSTART.md`, `docs/ARCHITECTURE.md` hay `PHASE.md`. Các file đó đã được gộp/chuẩn hóa
vào bộ docs `v1.0/docs/`. Dự án không dùng phase/roadmap làm source of truth; nhãn phase / M8 / M10 /
schema v3 / `mai-agent-v1` chỉ là mốc component/contract, **không** phải product version.

Khi tài liệu và code mâu thuẫn, áp dụng thứ tự source of truth trong `v1.0/README.md`:
interface → composition root (`orchestrator/stream_runtime.py`) → `services/` → `config/` YAML → tài liệu.

## Nguyên tắc DOCS-FIRST (BẮT BUỘC)

Trước **bất kỳ** thay đổi hệ thống nào (code, test, config, cấu trúc file):

1. Chuẩn hóa / cập nhật tài liệu liên quan **TRƯỚC** cho khớp với thay đổi định làm.
2. Confirm tài liệu với user.
3. Sau đó mới sửa hệ thống, và giữ code khớp đúng tài liệu vừa chốt.

Không bao giờ sửa code trước rồi mới vá tài liệu sau. Tài liệu mô tả hệ thống đang tồn tại, không mô tả roadmap.

## Ràng buộc bắt buộc (KHÔNG vi phạm)

1. **OS:** Windows 11 only. Dùng PowerShell, KHÔNG dùng Bash syntax.
2. **LLM backend:** llama.cpp (`llama-server.exe`). KHÔNG dùng Ollama, transformers hoặc vLLM.
3. **Ngôn ngữ:** Python 3.11+, type hints đầy đủ, async/await cho I/O.
4. **Interface-based:** service crossing subsystem phải implement interface trong `v1.0/interfaces/`.
5. **Feature toggle:** feature optional mới phải đăng ký với FeatureManager.
6. **Observable:** feature mới có ít nhất 1 metric.
7. **Config over code:** không hardcode threshold/magic number; đặt config production trong `v1.0/config/` YAML.
8. **Simplicity:** làm bản đơn giản, deterministic và test được trước.

## Ranh giới NGHIÊM CẤM

- KHÔNG tạo code chưa test được.
- KHÔNG tạo / sửa file ngoài scope task đang được giao.
- KHÔNG tự nhảy sang task tiếp theo khi task hiện tại chưa được user review.
- KHÔNG dùng Bash command trên Windows (rm, cp, mkdir -p, source, /) — dùng PowerShell.
- KHÔNG dùng SIGTERM/SIGKILL; process runtime sở hữu dùng `proc.terminate()`.
- KHÔNG sửa mô tả lịch sử v1.0.0 để làm như feature phát sinh sau baseline đã có sẵn.

## Workflow bắt buộc

Trước khi sửa code:

1. Đọc `v1.0/docs/00_V1_0_BASELINE.md`, `v1.0/docs/README.md`, rồi doc module liên quan task.
2. **Docs-first:** chuẩn hóa / cập nhật tài liệu liên quan trước.
3. List file sẽ tạo/sửa và test sẽ chạy.
4. Confirm với user rồi mới code.

Sau khi sửa code:

1. Chạy pytest targeted, show output; nếu đổi output/decision thì replay scenario deterministic tương ứng.
2. STOP và báo user review. KHÔNG tự động sang task tiếp theo.
3. Nếu thay đổi được chấp nhận để phát hành: tăng `v1.0/config/system.yaml::app.version`, thêm entry
   `v1.0/CHANGELOG.md`, cập nhật nhãn `Applies to` trong tài liệu bị ảnh hưởng — trong cùng một change.
