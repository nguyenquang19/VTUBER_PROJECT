# Tài liệu Mai V2

**Implementation generation:** `v2.0`

**Product version:** lấy duy nhất từ `config/system.yaml::app.version`, hiện là `1.4.3`

**Ngày đối chiếu:** 19/08/2026

Thư mục này chỉ có hai tài liệu kỹ thuật chính. Mọi hướng dẫn runtime, component, pipeline, cấu hình,
storage, vận hành, bảo mật, kiểm thử và known gaps đã được gộp vào một đặc tả để tránh sai lệch giữa
nhiều file.

| Tài liệu | Vai trò |
|---|---|
| [V1_BASELINE.md](V1_BASELINE.md) | Ảnh chụp lịch sử bất biến của release `1.0.0`, invariant và version policy |
| [MAI_V2_SYSTEM_SPEC.md](MAI_V2_SYSTEM_SPEC.md) | Nguồn sự thật duy nhất về implementation và hành vi hiện tại |
| [baselines/](baselines/) | Evidence máy đọc đã làm sạch; không phải hướng dẫn runtime |

Roadmap duy nhất nằm ở
[MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md](../MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md).
Blueprint chỉ quy định scope, thứ tự và acceptance gate tương lai; không chứng minh feature đã production.

## Thứ tự đọc

1. `V1_BASELINE.md` để khóa lịch sử, invariant và version policy.
2. `MAI_V2_SYSTEM_SPEC.md` để hiểu toàn bộ hiện trạng và tìm owner/file cần sửa.
3. Blueprint chỉ khi lập kế hoạch phase tiếp theo.
4. Trước khi thay đổi, đọc interface → composition root → implementation → YAML → impacted tests.

## Thứ tự nguồn sự thật

Khi có mâu thuẫn: interface/model bất biến → composition trong `orchestrator/stream_runtime.py` →
implementation service → production YAML → tests → `MAI_V2_SYSTEM_SPEC.md` → README/roadmap.
Phải báo conflict trước khi sửa; không âm thầm chọn tài liệu thuận tiện hơn.

## Quy tắc giữ tài liệu sạch

- Không tạo thêm tài liệu theo phase, milestone, component hoặc bản audit riêng.
- Sửa behavior hiện tại thì cập nhật `MAI_V2_SYSTEM_SPEC.md` trong cùng change.
- Sửa scope/thứ tự tương lai thì cập nhật blueprint, không đưa kế hoạch vào System Spec như production.
- Không sửa capability inventory lịch sử trong `V1_BASELINE.md`.
- Evidence JSON tiếp tục nằm trong `docs/baselines/`; không sao chép số liệu vào nhiều tài liệu.
- Link, version, feature inventory và config inventory phải qua documentation guard.
