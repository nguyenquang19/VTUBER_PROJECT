# Mai V2 — AI VTuber

Mai là AI VTuber tiếng Việt chạy local trên Windows 11. V2 mở rộng runtime host hội thoại hiện có thành
world-aware autonomous agent bằng một closed loop có perception, world/self state, dynamic capability,
typed action, executor, verification và commit/rollback.

## Version layout

```text
ver/
└ v1.0/   frozen source snapshot; owner-managed archive
v2.0/     current implementation working tree at repository root
```

`ver/v1.0/` được giữ bất biến. Mọi thay đổi V2 thực hiện trong root `v2.0/`. Coding agent không tự tạo
version mới trong `ver/`; owner archive vào đó khi nâng major version. Virtual environment, model,
logs, secrets và runtime data không được nhân bản theo version; tái tạo environment từ lockfile và cấu
hình resource path riêng.

Tên thư mục `v2.0`, blueprint version và product version là ba trục khác nhau. Product version hiện hành
luôn lấy từ `v2.0/config/system.yaml::app.version`; không đổi thành 2.0.0 chỉ vì tạo working tree.

## Tài liệu

- [V2 working-tree instructions](v2.0/AGENTS.md)
- [MAI V2 Master Implementation Blueprint v2.0](v2.0/MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md)
- [Runtime README](v2.0/README.md)
- [Technical documentation index](v2.0/docs/README.md)
- [Frozen V1 baseline](ver/v1.0/docs/00_V1_0_BASELINE.md)

Blueprint khóa scope và thứ tự migration. Tài liệu runtime chỉ mô tả capability đã triển khai; mỗi task
chỉ thực hiện một phase, chạy targeted test + impacted V1 regression rồi dừng để review.

## Chạy runtime hiện hành

```powershell
Set-Location .\v2.0
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
.\scripts\start_live.ps1 -Platform youtube -VideoId "VIDEO_ID"
```

Dashboard mặc định: `http://127.0.0.1:7860`.

## Invariants chính

- World Model không chọn action.
- LLM không định nghĩa available capabilities.
- Director không gọi external tool trực tiếp.
- Không assume success trước verification.
- Không commit world/business state từ lời LLM.
- Hard safety/permission/transaction rules thắng soft policy.
- Không thêm logic V3.
