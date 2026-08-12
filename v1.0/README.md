# Mai v1.0.0 — AI VTuber runtime

> **Current product release:** `1.0.2` (patch trên frozen baseline `1.0.0`, 2026-08-12).
>
> Mọi thay đổi product được chấp nhận sau baseline phải tăng version và cập nhật `CHANGELOG.md`.

Mai là hệ thống AI VTuber chạy local trên Windows 11. Backend hội thoại là `llama.cpp`, đầu vào
hiện tại là YouTube/Discord chat, đầu ra là text, audio VieNeu-TTS và subtitle fallback. Runtime có
Director quyết định hành động, mood Hybrid, memory tùy chọn, transaction tại ranh giới delivery,
operator dashboard và bộ công cụ đánh giá/vận hành.

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

## Bắt đầu đọc tài liệu

- [Baseline Mai v1.0.0 và version policy](docs/00_V1_0_BASELINE.md)
- [Mục lục kỹ thuật](docs/README.md)
- [Tổng quan hệ thống](docs/01_SYSTEM_OVERVIEW.md)
- [Pipeline dữ liệu end-to-end](docs/02_DATA_PIPELINE.md)
- [Tra cứu component](docs/03_COMPONENT_REFERENCE.md)
- [Vận hành và sửa lỗi](docs/06_OPERATIONS_AND_TROUBLESHOOTING.md)

## Nguyên tắc source of truth

Tài liệu mô tả code đang chạy, không mô tả roadmap. Khi tài liệu và code mâu thuẫn, ưu tiên theo
thứ tự: model/interface trong `interfaces/` → runtime composition trong
`orchestrator/stream_runtime.py` → implementation trong `services/` → YAML trong `config/` → tài
liệu này. Mọi thay đổi contract hoặc pipeline phải cập nhật tài liệu tương ứng trong cùng change.

Product version lấy duy nhất từ `config/system.yaml::app.version`. Các nhãn Mood v2, turn schema v3,
M8/M10 hoặc `mai-agent-v1` là version/mốc của component, không phải product release.
