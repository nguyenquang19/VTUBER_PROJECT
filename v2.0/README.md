# Mai V2 working tree — AI VTuber runtime

> **Implementation generation:** `v2.0` — development working tree.
>
> **Inherited runtime release:** `1.4.3` (patch trên frozen baseline `1.0.0`, 2026-08-14).
>
> **Implementation status:** nhiều khối V2 đã có mã/shadow, nhưng takeover thật, action adapter composition
> và external action closed loop chưa hoàn tất. Chưa đủ điều kiện phát hành Mai `2.0.0`.
>
> Mọi thay đổi product được chấp nhận sau baseline phải tăng version và cập nhật `CHANGELOG.md`.

Blueprint triển khai canonical: [MAI V2 — Master Implementation Blueprint v2.0](MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md).
Source snapshot V1 nằm tại `../ver/v1.0/` và không được sửa trực tiếp. Chỉ triển khai một phase mỗi task
trong working tree này; giữ V1 fallback cho tới khi shadow/rollout gate tương ứng hoàn tất.

Mai là hệ thống AI VTuber chạy local trên Windows 11. Backend hội thoại là `llama.cpp`, đầu vào
hiện tại là YouTube/Discord chat, đầu ra là text, audio VieNeu-TTS và subtitle fallback. Runtime có
Director quyết định hành động, mood Hybrid, memory tùy chọn, transaction tại ranh giới delivery,
operator dashboard và bộ công cụ đánh giá/vận hành.

## Tạo môi trường chuẩn

Yêu cầu Windows 11 và `uv`. Script bootstrap cài Python `3.11.15`, dựng môi trường tạm, cài đúng lock
file, xác minh dependency rồi mới thay `venv`; môi trường cũ được giữ làm backup.

```powershell
.\scripts\bootstrap_environment.ps1
.\scripts\check_environment.ps1 -SkipLlamaHealth
```

Không dùng `requirements.txt` để dựng môi trường phát triển/production tái lập; nguồn dependency chuẩn
là `requirements.lock.txt`.

## Chạy live

```powershell
# YouTube
.\scripts\start_live.ps1 -Platform youtube -VideoId "VIDEO_ID"

# Discord
$env:DISCORD_BOT_TOKEN = "YOUR_TOKEN"
.\scripts\start_live.ps1 -Platform discord

# YouTube + Discord
$env:DISCORD_BOT_TOKEN = "YOUR_TOKEN"
.\scripts\start_live.ps1 -Platform youtube -VideoId "VIDEO_ID" -WithDiscord
```

Launcher mặc định bật TTS và dashboard. Thêm `-Memory` để bật semantic memory. Dashboard ở
`http://127.0.0.1:7860`; dashboard cũ luôn còn tại `/legacy`. OBS có thể đọc subtitle fallback từ
`logs\live\subtitle.txt`.

`python -m orchestrator.main` là entrypoint bootstrap cũ và từ `1.4.1` chỉ fail-fast với hướng dẫn;
không dùng lệnh đó để chạy live. Production chỉ đi qua launcher PowerShell ở trên hoặc hai platform
entrypoint mà launcher gọi.

## Bắt đầu đọc tài liệu

- [Mục lục tối giản](docs/README.md)
- [Baseline V1 bất biến](docs/V1_BASELINE.md)
- [Đặc tả hệ thống V2 hiện tại](docs/MAI_V2_SYSTEM_SPEC.md)
- [Blueprint V2 — roadmap, không phải current truth](MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md)

## Nguyên tắc source of truth

Các tài liệu runtime mô tả code đang chạy; blueprint chỉ khóa scope và thứ tự migration, không được dùng
để tuyên bố feature chưa triển khai là production. Khi tài liệu runtime và code mâu thuẫn, ưu tiên theo
thứ tự: model/interface trong `interfaces/` → runtime composition trong
`orchestrator/stream_runtime.py` → implementation trong `services/` → YAML trong `config/` → tests →
`docs/MAI_V2_SYSTEM_SPEC.md` → tài liệu giới thiệu/roadmap. Mọi thay đổi contract hoặc pipeline phải cập
nhật System Spec trong cùng change.

Product version lấy duy nhất từ `config/system.yaml::app.version`. Các nhãn Mood v2, turn schema v3,
M8/M10, `mai-agent-v1`, tên thư mục `v2.0` hoặc version blueprint là version/mốc khác, không phải
product release.
