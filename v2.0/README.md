# Mai V2 working tree — AI VTuber runtime

> **Implementation generation:** `v2.0` — development working tree.
>
> **Inherited runtime release:** `1.4.3` (patch trên frozen baseline `1.0.0`, 2026-08-14).
>
> **Implementation status:** Phase 1–15 đã đóng gate kỹ thuật. Structure normalization S0–S4 đã được
> checkpoint; S2 tại `d02c84e`, S3 canonical Cognition tại `1c6d9d6`, S4 Turn Kernel tại `361bc44`.
> S5 canonical execution/outcome đã triển khai và đang chờ owner review; Compatibility vẫn là public owner.
> Live runtime dùng một `CognitiveContextBuilder`,
> một `CognitiveModelAdapter`, còn
> Agent/World/Perception event đi qua `CanonicalEventNormalizer → CanonicalEventIngress → AuthoritativeStateReducer`.
> Director V2 đang ở strict primary mode cho test-cutover; Cognitive Brain là subordinate shadow worker
> mặc định tắt dưới Turn Kernel và chưa có takeover authority.
> OBS scene/perception, closed-loop canary và release `2.0.0` vẫn chưa đạt live gate.
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
# Dashboard control credential (bắt buộc vì launcher mặc định bật dashboard)
$env:MAI_DASHBOARD_CONTROL_TOKEN = "GENERATE_A_LONG_RANDOM_SECRET"

# YouTube
.\scripts\start_live.ps1 -Platform youtube -VideoId "VIDEO_ID"

# Discord
$env:DISCORD_BOT_TOKEN = "YOUR_TOKEN"
.\scripts\start_live.ps1 -Platform discord

# YouTube + Discord
$env:DISCORD_BOT_TOKEN = "YOUR_TOKEN"
.\scripts\start_live.ps1 -Platform youtube -VideoId "VIDEO_ID" -WithDiscord
```

Runtime không tự nạp `.env`. `MAI_DASHBOARD_CONTROL_TOKEN` bắt buộc khi dashboard bật;
`DISCORD_BOT_TOKEN` chỉ bắt buộc cho phiên có Discord; `OBS_WEBSOCKET_PASSWORD` chỉ bắt buộc khi
operator chủ động bật `obs_scene_executor`. Các giá trị phải được PowerShell hoặc secret store truyền vào process, không ghi vào
YAML, CLI argument, `.env.example` hoặc Git.

Launcher mặc định bật TTS và dashboard. Thêm `-Memory` để bật semantic memory. Dashboard ở
`http://127.0.0.1:7860`, chỉ bind loopback và yêu cầu operator token cho mọi lệnh thay đổi trạng thái;
UI sẽ hỏi token ở lần điều khiển đầu tiên và không nhúng secret vào HTML. Dashboard cũ luôn còn tại
`/legacy`. OBS có thể đọc subtitle fallback từ
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

`config/state.yaml` là canonical owner cho giới hạn ingress/Agent/World/Self/Relationship sau S2;
`config/cognition.yaml` là owner duy nhất cho context projection, typed Cognitive Context và Brain adapter.
`config/agent_state.yaml`, `config/relationships.yaml` và các implementation import path cũ chỉ còn là
compatibility surface có removal wave S8; không được chỉnh chúng như một bộ cấu hình độc lập.
