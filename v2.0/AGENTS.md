# Project: Mai V2 working tree - AI VTuber

## Implementation generation

- Working tree hiện tại: root `v2.0/`.
- Frozen source snapshot: `ver/v1.0/`; không sửa trực tiếp.
- Canonical migration plan: `MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md`.
- Chỉ làm đúng một phase mỗi task và dừng sau report/test để user review.
- Tên thư mục `v2.0` không tự động đổi product version. Product version chỉ lấy từ
  `config/system.yaml::app.version` và hiện vẫn là `1.4.3` cho tới khi một release change được chấp nhận.

## Product baseline

- Current frozen baseline: `1.0.0`; product version hiện tại `1.4.3` (patch). Source:
  `config/system.yaml::app.version`.
- Đọc `docs/00_V1_0_BASELINE.md` trước mọi task để phân biệt capability production, optional và
  interface-only.
- Mood v2, schema v3, M8/M10 và `mai-agent-v1` là nhãn component/contract, không phải product version.
- Mọi product change được chấp nhận sau baseline phải tăng ít nhất patch version, thêm
  `CHANGELOG.md`, cập nhật tài liệu áp dụng và có regression evidence.
- Không sửa mô tả lịch sử v1.0.0 để làm như feature phát sinh sau này đã có trong baseline.

## Tài liệu chính

- `README.md` — cách chạy và thứ tự source of truth.
- `MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md` — scope khóa cứng, phase order và acceptance gates V2.
- `docs/00_V1_0_BASELINE.md` — release baseline, capability, invariant và version policy.
- `docs/README.md` — mục lục tài liệu chuẩn hóa; chọn tài liệu theo loại task.
- `docs/01_SYSTEM_OVERVIEW.md` — phạm vi, boundary, ownership và lifecycle.
- `docs/02_DATA_PIPELINE.md` — pipeline input → decision → generation → delivery → commit.
- `docs/03_COMPONENT_REFERENCE.md` — component/file chịu trách nhiệm.
- `docs/04_DATA_AND_STORAGE.md` — contract dữ liệu, bounded state và commit semantics.
- `docs/05_CONFIGURATION.md` — YAML, feature toggle và quy trình tune an toàn.
- `docs/06_OPERATIONS_AND_TROUBLESHOOTING.md` — vận hành và chẩn đoán.
- `docs/07_TESTING_AND_EXTENSION.md` — test và quy trình mở rộng.
- `docs/08_SECURITY_RECOVERY.md` — an toàn, PII, rollback và recovery.

Không yêu cầu `PHASE.md`, `docs/QUICKSTART.md` hoặc `docs/ARCHITECTURE.md`; các tài liệu cũ đó đã được
chuẩn hóa và gộp vào bộ tài liệu trên. Blueprint V2 là source of truth cho scope và thứ tự migration;
code/interfaces/tests/config cùng tài liệu `00`–`08` là source of truth cho behavior đã triển khai.
Blueprint không được dùng để tuyên bố feature chưa code là production. Khi tài liệu runtime và code
mâu thuẫn, áp dụng thứ tự source of truth trong `README.md` và báo conflict trước khi sửa.

Conversation continuity và Thread Engine dùng trực tiếp các mục tương ứng trong `docs/02_DATA_PIPELINE.md`,
`docs/03_COMPONENT_REFERENCE.md`, `docs/04_DATA_AND_STORAGE.md`, `docs/05_CONFIGURATION.md` và
`docs/07_TESTING_AND_EXTENSION.md`; không tìm kế hoạch/architecture cũ đã bị gộp hoặc xóa.

## Ràng buộc bắt buộc

1. **OS:** Windows 11 only. Dùng PowerShell, không dùng Bash syntax.
2. **LLM backend:** llama.cpp (`llama-server.exe`), không dùng Ollama, transformers hoặc vLLM.
3. **Ngôn ngữ:** Python 3.11+, type hints đầy đủ, async/await cho I/O.
4. **Interface-based:** service mới crossing subsystem phải implement interface trong `interfaces/`.
5. **Feature toggle:** feature tùy chọn mới phải đăng ký với `FeatureManager`.
6. **Observable:** feature mới có ít nhất một metric.
7. **Config over code:** không hardcode threshold/magic number; đặt cấu hình production trong YAML.
8. **Simplicity:** làm bản đơn giản, deterministic và test được trước.

## Ranh giới nghiêm cấm

- Không tạo code chưa test được.
- Không tạo file ngoài scope task.
- Không tự chuyển sang task khác khi task hiện tại chưa được user review.
- Không dùng Bash command trên Windows.
- Không dùng SIGTERM/SIGKILL; process Windows do runtime sở hữu dùng `proc.terminate()`.
- Không sửa hoặc copy ngược code V2 vào frozen snapshot `ver/v1.0/`.
- Không tự tạo version mới trong `ver/`; owner quản lý archive khi nâng major version.
- Không copy code từ phiên bản deprecated khác ngoài snapshot nền đã được dùng để khởi tạo working tree.
- Không đặt tên file test theo phase/milestone (`_m*`, `phase*`, `m8_...`); đặt theo component/hành vi
  (xem `docs/07` §1.1). Không thêm test-runner ad-hoc song song với `pytest` + marker.

## Workflow bắt buộc

**Docs-first (bắt buộc):** trước bất kỳ thay đổi hệ thống nào (code, test, config, cấu trúc file), phải
chuẩn hóa/cập nhật tài liệu liên quan cho khớp thay đổi định làm rồi mới sửa hệ thống. Không sửa code
trước rồi vá tài liệu sau.

Trước khi sửa code:

1. Xác nhận phase hiện tại từ blueprint; không tự chuyển phase.
2. Đọc `docs/00_V1_0_BASELINE.md`, `docs/README.md`, tài liệu module, code/config và impacted tests.
3. Báo files đọc, files tạo/sửa, contracts, tests và migration risks; báo conflict trước khi sửa.
4. Chuẩn hóa/cập nhật tài liệu liên quan trước (docs-first).
5. Chỉ code scope của phase hiện tại sau khi user xác nhận.

Sau khi code:

1. Chạy test targeted và impacted V1 regression, hiển thị output.
2. Nếu thay đổi output/decision, replay corpus hoặc scenario deterministic tương ứng.
3. Báo changed files, pass/fail, metrics và known risks.
4. Dừng và báo user review; không tự chuyển sang phase/task tiếp theo.

Nếu thay đổi được user chấp nhận để phát hành, xác định patch/minor/major theo baseline, tăng
`config/system.yaml::app.version` và cập nhật `CHANGELOG.md` trong cùng change.
