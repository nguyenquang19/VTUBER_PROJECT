# Mai V2 — AI VTuber

Mai là AI VTuber tiếng Việt chạy **local trên Windows 11**. Backend hội thoại `llama.cpp`, đầu vào chat
YouTube/Discord, đầu ra text + audio VieNeu-TTS + subtitle + avatar VTube Studio. Runtime có Director quyết
định hành động, mood Hybrid, memory tùy chọn, transaction tại ranh giới delivery, dashboard operator và bộ
đánh giá offline.

## Version layout

```text
ver/
└ v1.0/   snapshot đóng băng; chỉ dùng baseline/regression/reference
v2.0/     working tree hiện hành
```

`ver/v1.0/` giữ bất biến. Mọi thay đổi thực hiện trong `v2.0/`. Product version lấy **duy nhất** từ
`v2.0/config/system.yaml::app.version`. Venv, model, logs, secrets và runtime data không nhân bản theo
version — tái tạo từ lockfile.

## Tài liệu

| File | Vai |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Chỉ dẫn cho coding agent (Codex/Claude): workflow, ràng buộc, review loop. `CLAUDE.md` trỏ về đây. |
| [`v2.0/README.md`](v2.0/README.md) | README runtime: tạo môi trường, chạy live |
| [`v2.0/docs/MAI_V2_SYSTEM_SPEC.md`](v2.0/docs/MAI_V2_SYSTEM_SPEC.md) | Nguồn sự thật về hành vi hiện tại |
| [`v2.0/docs/ROADMAP.md`](v2.0/docs/ROADMAP.md) | Scope + kế hoạch phase tương lai |
| [`v2.0/docs/V1_BASELINE.md`](v2.0/docs/V1_BASELINE.md) | Lịch sử đóng băng release `1.0.0` — bất biến |
| [`v2.0/CHANGELOG.md`](v2.0/CHANGELOG.md) | Lịch sử các release |

Nguồn sự thật khi mâu thuẫn: `interfaces/` → `orchestrator/stream_runtime.py` → `services/` →
`config/*.yaml` → `tests/` → SYSTEM_SPEC → README/ROADMAP.

## Chạy nhanh

```powershell
Set-Location .\v2.0
.\scripts\bootstrap_environment.ps1
$env:MAI_DASHBOARD_CONTROL_TOKEN = "GENERATE_A_LONG_RANDOM_SECRET"
.\scripts\start_live.ps1 -Platform youtube -VideoId "VIDEO_ID"
```

Chi tiết môi trường, Discord, memory, dashboard: xem [`v2.0/README.md`](v2.0/README.md).

## Invariants chính

- Tick-driven 1.5s, không reactive. Quyết định ≠ sinh chữ. Tạo câu ≠ đã nói (`verify → commit → project`).
- World Model không chọn action. LLM không định nghĩa capability. Director không gọi external tool trực tiếp.
- Không assume success trước verification. Hard safety/permission/transaction thắng soft policy.
- Threshold/TTL/cooldown/weight ở YAML. Feature mới: đăng ký `FeatureManager` + có metric. Không thêm V3.
