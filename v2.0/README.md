# Mai V2 — AI VTuber runtime

Mai là AI VTuber tiếng Việt chạy **local trên Windows 11**. Backend hội thoại `llama.cpp`, đầu vào chat
YouTube/Discord, đầu ra text + audio VieNeu-TTS + subtitle + avatar VTube Studio. Runtime có Director
quyết định hành động, mood Hybrid 10Hz, memory tùy chọn, transaction tại ranh giới delivery, dashboard
operator và bộ đánh giá offline.

- **Product version:** lấy duy nhất từ `config/system.yaml::app.version` (hiện `1.4.3`).
- **Working tree:** `v2.0/`. Frozen snapshot: `ver/v1.0/` — không sửa trực tiếp.

## Tài liệu

Ba tài liệu chính thức, không tạo docs lẻ:

| Tài liệu | Vai trò |
|---|---|
| [`docs/MAI_V2_SYSTEM_SPEC.md`](docs/MAI_V2_SYSTEM_SPEC.md) | **Nguồn sự thật về hành vi hiện tại** — kiến trúc, luồng, invariant, trạng thái |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Scope tương lai + kế hoạch phase (Brain-primary, naturalness) |
| [`docs/V1_BASELINE.md`](docs/V1_BASELINE.md) | Lịch sử đóng băng release `1.0.0` — bất biến |

Cho agent: [`AGENTS.md`](AGENTS.md) (workflow + ràng buộc). Nguồn sự thật khi mâu thuẫn:
`interfaces/` → `orchestrator/stream_runtime.py` → `services/` → `config/*.yaml` → `tests/` → SYSTEM_SPEC.

## Tạo môi trường

Yêu cầu Windows 11 + `uv`. Bootstrap cài Python `3.11.15`, dựng staging, cài `requirements.lock.txt`,
`pip check`, rồi mới thay `venv` (giữ backup).

```powershell
.\scripts\bootstrap_environment.ps1
.\scripts\check_environment.ps1 -SkipLlamaHealth
```

Nguồn dependency chuẩn là `requirements.lock.txt`, không dùng `requirements.txt` để dựng môi trường tái lập.

## Chạy live

```powershell
$env:MAI_DASHBOARD_CONTROL_TOKEN = "GENERATE_A_LONG_RANDOM_SECRET"   # bắt buộc khi dashboard bật

# YouTube
.\scripts\start_live.ps1 -Platform youtube -VideoId "VIDEO_ID"

# Discord
$env:DISCORD_BOT_TOKEN = "YOUR_TOKEN"
.\scripts\start_live.ps1 -Platform discord

# YouTube + Discord + Memory
$env:DISCORD_BOT_TOKEN = "YOUR_TOKEN"
.\scripts\start_live.ps1 -Platform youtube -VideoId "VIDEO_ID" -WithDiscord -Memory
```

Launcher mặc định bật TTS + dashboard. `-Memory` bật semantic memory. Dashboard `http://127.0.0.1:7860`
(loopback, token-gated). Credential chỉ truyền qua environment/secret store lúc chạy, không ghi vào
YAML/CLI/`.env.example`/Git.

`python -m orchestrator.main` là entrypoint cũ, từ `1.4.1` chỉ fail-fast — không dùng để chạy live.

## Invariants chính

- Tick-driven 1.5s, không reactive. Quyết định ≠ sinh chữ. Tạo câu ≠ đã nói (`verify → commit → project`).
- World Model không chọn action. LLM không định nghĩa capability. Director không gọi external tool trực tiếp.
- Không assume success trước verification. Không commit world/business state từ lời LLM.
- Hard safety/permission/transaction thắng soft policy. Không thêm logic V3.
- Threshold/TTL/cooldown/weight ở YAML. Feature mới: đăng ký `FeatureManager` + có metric.
