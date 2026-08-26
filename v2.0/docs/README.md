# Tài liệu Mai V2

Chỉ ba nguồn chính thức. File này chỉ điều hướng, không phải nguồn thứ tư. Mọi hướng dẫn runtime,
component, pipeline, cấu hình, storage, vận hành, bảo mật, kiểm thử và known gaps nằm trong SYSTEM_SPEC.

| Tài liệu | Vai trò |
|---|---|
| [MAI_V2_SYSTEM_SPEC.md](MAI_V2_SYSTEM_SPEC.md) | Nguồn sự thật duy nhất về implementation và hành vi hiện tại |
| [ROADMAP.md](ROADMAP.md) | Scope, thứ tự phase và acceptance gate tương lai (Brain-primary, naturalness) |
| [V1_BASELINE.md](V1_BASELINE.md) | Ảnh chụp lịch sử bất biến release `1.0.0`, invariant, version policy |
| [baselines/](baselines/) | Evidence máy đọc; không phải hướng dẫn runtime |

## Thứ tự đọc

1. `V1_BASELINE.md` — khóa lịch sử, invariant, version policy.
2. `MAI_V2_SYSTEM_SPEC.md` — hiểu hiện trạng, tìm owner/file cần sửa.
3. `ROADMAP.md` — chỉ khi lập kế hoạch phase tiếp theo.
4. Trước khi sửa: interface → composition root → implementation → YAML → impacted tests.

## Thứ tự nguồn sự thật

Khi mâu thuẫn: `interfaces/` → `orchestrator/stream_runtime.py` → `services/` → `config/*.yaml` →
`tests/` → `MAI_V2_SYSTEM_SPEC.md` → README/ROADMAP. Báo conflict trước khi sửa.

## Quy tắc giữ tài liệu sạch

- Không tạo docs theo phase/milestone/component/audit riêng. Không giữ draft/checklist/tuning plan ở root.
- Sửa behavior hiện tại → cập nhật `MAI_V2_SYSTEM_SPEC.md` cùng change.
- Sửa scope tương lai → cập nhật `ROADMAP.md`, không đưa kế hoạch vào SYSTEM_SPEC như production.
- Không sửa `V1_BASELINE.md` (lịch sử đóng băng). Evidence JSON ở `baselines/`, không chép số nhiều nơi.
- Comment/docstring chỉ mô tả invariant/ownership/failure/lý do hiện tại — không lời hứa tương lai.
