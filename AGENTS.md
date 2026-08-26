# AGENTS.md — Mai V2 (AI VTuber)

File chỉ dẫn canonical cho mọi coding agent. `CLAUDE.md` trỏ về đây. Đọc hết file này trước khi sửa bất cứ gì.

## Dự án là gì

AI VTuber tiếng Việt, chạy **local trên Windows 11**, backend `llama.cpp`. Kiến trúc, luồng và trạng thái
đầy đủ: `docs/MAI_V2_SYSTEM_SPEC.md`.

## Version layout

- `v2.0/` — working tree hiện hành (ở repo root nếu bạn đang trong đó).
- `ver/v1.0/` — snapshot đóng băng; chỉ dùng baseline/regression/reference. **Không sửa trực tiếp.**
- Product version lấy **duy nhất** từ `config/system.yaml::app.version`. Tên thư mục `v2.0`, version
  blueprint, schema/component version là các trục khác — không phải product release.
- Agent **không** tự tạo version mới trong `ver/`.

## Thứ tự đọc bắt buộc

1. File này (`AGENTS.md`).
2. `docs/MAI_V2_SYSTEM_SPEC.md` — hành vi hiện tại, tìm owner/file cần sửa.
3. `docs/V1_BASELINE.md` — invariant, version policy đóng băng.
4. `docs/ROADMAP.md` — chỉ khi lập kế hoạch phase tiếp theo.
5. Trước khi sửa: interface → composition root → implementation → YAML → impacted tests của phase.

## Thứ tự nguồn sự thật

Khi mâu thuẫn: `interfaces/` → `orchestrator/stream_runtime.py` → `services/` → `config/*.yaml` →
`tests/` → `docs/MAI_V2_SYSTEM_SPEC.md` → README/ROADMAP. **Báo conflict trước khi sửa**, không tự đoán,
không rewrite lịch sử V1.

## Ràng buộc bắt buộc

1. **Windows 11 / PowerShell** — không dùng Bash syntax trong script.
2. **LLM backend là `llama.cpp`** (`llama-server.exe`) — không Ollama/transformers/vLLM.
3. **Python 3.11+**, type hints đầy đủ, `async/await` cho I/O.
4. Service cross-subsystem phải implement interface trong `interfaces/`.
5. Feature tùy chọn mới phải đăng ký `FeatureManager` và **có metric**.
6. Threshold/TTL/cooldown/weight production ở **YAML**, không hardcode.
7. Memory/relationship/journal lưu **bí danh + ý nghĩa, không PII**.
8. Credential chỉ qua environment/secret store lúc chạy — không ghi YAML/CLI/`.env.example`/Git.
9. Ưu tiên implementation đơn giản, deterministic, test được. **Không thêm scope V3.**

## Invariants runtime (đừng phá)

- **Tick-driven 1.5s**, không reactive. **Quyết định ≠ sinh chữ.** **Tạo câu ≠ đã nói**
  (`verify → commit → project`).
- World Model không chọn action. LLM không định nghĩa capability. Director không gọi external tool trực tiếp.
- Hard safety/permission/transaction thắng soft policy. Không commit state trước verified success.
- **Không gỡ legacy/V1 fallback trước shadow validation.**

## Bẫy naming (đọc SYSTEM_SPEC §9 trước khi động vào Director/Kernel)

- `public_owner = COMPATIBILITY` nghĩa là "đường DirectorLoop, **không phải** Brain" — không phải legacy
  Director đang quyết. Bên trong DirectorLoop, **Director V2 là primary** (`ownership_mode: primary`),
  legacy chỉ là hard-preempt + fallback.
- `cognitive_brain_shadow` = Brain offer **sau** execute, không public.
- Chi tiết thuật toán quyết định: SYSTEM_SPEC §4.2.

## Workflow — mỗi task đúng một phase

**Trước khi code:** đọc → báo files/contracts/tests/risks → **docs-first** (cập nhật SYSTEM_SPEC cho behavior,
ROADMAP cho scope, **trong cùng change**) → xác nhận với user.

**Code:** đúng một phase. Không gộp nhiều phase. Không tự chuyển phase.

**Sau khi code:** targeted tests + impacted V1 regression (+ replay nếu cần) → báo metrics/risks → **STOP**
để user review.

```powershell
# targeted (vùng vừa sửa)
python -m pytest tests/unit/<path> tests/integration/<path> -q
# offline đầy đủ (skip test cần llama-server thật + test chậm)
python -m pytest tests -m "not llm and not slow" -q
```

## Tự soát trước khi báo "xong"

Rủi ro lớn nhất khi làm nhanh là báo đạt về hình thức trong khi bằng chứng thật thiếu. Trước khi coi một
change là hoàn tất, tự trả lời — nếu có mục FAIL thì chưa xong:

1. **Docs khớp behavior** — đổi hành vi runtime → đã cập nhật `docs/MAI_V2_SYSTEM_SPEC.md` cùng change?
   Đổi scope → `docs/ROADMAP.md`? Không tạo docs lẻ, không sửa `V1_BASELINE.md`.
2. **Flag + metric** — feature mới đã đăng ký `FeatureManager` và **có metric**? Tắt thì về hành vi cũ?
3. **Config-over-code** — threshold/TTL/cooldown/weight ở YAML đúng owner, không hardcode trong `.py`?
4. **An toàn & PII** — không gỡ legacy/V1 fallback trước shadow? safety/transaction vẫn thắng soft policy?
   memory/relationship/journal/log/dataset chỉ lưu bí danh + ý nghĩa, **không PII/transcript/định danh thật**?
   credential không lọt vào YAML/CLI/Git?
5. **Contract** — service cross-subsystem implement `interfaces/`? Không phá thứ tự nguồn sự thật? Mâu thuẫn
   docs↔code thì đã **báo** thay vì âm thầm chọn bên?
6. **Phạm vi** — chỉ một phase/một mục tiêu? Không thêm V3? Không cho LLM định nghĩa capability?

## Review loop — Codex code, Claude review

Dự án chạy theo cặp: **Codex viết code, Claude review**. Cả hai đọc file này. Mỗi phase một vòng, một branch.

```
bạn: chọn 1 phase (ROADMAP) + tạo branch
  → CODEX: đọc AGENTS.md + SYSTEM_SPEC + phase → báo files/contract/tests/risks → (bạn xác nhận)
           → docs-first (cập nhật SYSTEM_SPEC) → code đúng 1 phase → test → tự soát 6 mục → DỪNG
  → CLAUDE: đọc `git diff` (không đọc mô tả) → đối chiếu SYSTEM_SPEC + săn lỗi đặc thù + chạy regression
           → chấm: DUYỆT / SỬA RỒI DUYỆT / TRẢ LẠI
  → nếu TRẢ LẠI: đưa findings nguyên văn về Codex → lặp lại bước code
  → nếu DUYỆT: bạn commit phase → sang phase kế
```

Nguyên tắc giữ loop sạch:

- **Branch mỗi phase** — diff gọn, review rõ, quay lui dễ.
- **Đọc diff, không đọc mô tả** — review cái *thật sự* sửa, không phải cái coder *nói* đã sửa.
- **Docs-first là điều kiện duyệt** — đổi behavior mà không cập nhật `MAI_V2_SYSTEM_SPEC.md` → trả lại.
- **Một phase = một review = một commit.** Không để chạy nhiều phase rồi mới review (mất khả năng cô lập lỗi).
- Findings của reviewer đưa **nguyên văn** lại cho coder, không diễn giải lại.

Review phải săn đúng lỗi đặc thù codebase (bẫy naming Director/Brain §9, gỡ fallback quá sớm, thiếu metric,
hardcode threshold, rò PII, phá thứ tự `verify → commit → project`) — không chỉ đọc cho có.

## Quy tắc tài liệu

- Không tạo docs theo phase/milestone/component/audit riêng. Không giữ draft/checklist/tuning plan ở root.
- Sửa behavior → `docs/MAI_V2_SYSTEM_SPEC.md` cùng change. Sửa scope tương lai → `docs/ROADMAP.md`.
- Không sửa `docs/V1_BASELINE.md`. Comment/docstring chỉ mô tả invariant/ownership/failure/lý do hiện tại.
