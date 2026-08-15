# Plan: Tune sampling cho Mai (giao cho Claude Code)

> Máy Windows, có `llama-server.exe` + Gemma 12B + GPU. Mục tiêu: chọn bộ sampling
> làm Mai nói tự nhiên/đỡ "sặc AI" nhất, đo bằng replay corpus, rồi phát hành như patch.
> Tooling đã có sẵn (build ở 1.2.0): `scripts/sampling_sweep.py`,
> `scripts/sample_conversation.py`, `config/sampling_sweep.yaml`.

## Bối cảnh (đọc trước)

- Đọc `AGENTS.md` + `docs/00_V1_0_BASELINE.md` §8 (version policy) trước khi làm.
- Sampling hiện tại nằm ở `config/models.yaml::llm_main` (temperature, min_p,
  repeat_penalty, presence_penalty, frequency_penalty, top_p, top_k).
- Nghi vấn hiện có: `presence_penalty 0.3` + `frequency_penalty 0.15` hơi nặng, có thể
  ép model dùng từ gượng → nghe máy móc. Cần kiểm bằng số + tai, không đoán.
- Thước đo trong report replay: `distinct_2` (đa dạng, cao = đỡ nhạt), `exact_repetition`
  (thấp = ít lặp), `assistant_register`/`meta_leak` flags (thấp = ít giọng AI),
  `avg_words` (nhắm ~8–18), `turn_latency_p95` (không được tệ đi).

## Ràng buộc bắt buộc

1. Windows/PowerShell, không dùng Bash.
2. KHÔNG tự commit và KHÔNG bump version cho tới khi user nghe `sample_convo` và duyệt.
3. Chỉ đụng `config/models.yaml::llm_main` sampling; không sửa runtime/interface/contract khác.
4. Số auto-pick chỉ là gợi ý — quyết định cuối phải qua đọc `operator_review_sample` + user nghe.
5. Giữ `inject_mood_directive: true` (chưa fine-tune, không đổi).

## Điều kiện tiên quyết (kiểm trước khi chạy)

- [ ] `llama-server` đang chạy đúng endpoint trong `models.yaml::llm_main` (port 8080).
- [ ] Có replay corpus. Ưu tiên `logs/evaluation/youtube_replay_real_llm_full.json`; nếu
      không có, tạo bằng `scripts/simulate_youtube_replay.py` hoặc dùng corpus chat thật đã có.
- [ ] Python deps đủ (httpx, pydantic, structlog, pyyaml…). Thiếu thì cài theo `requirements.txt`.
- [ ] Git sạch (để revert dễ nếu config mới dở).

## Các bước thực thi

### B1 — Chạy sweep (KHÔNG apply vội)

```powershell
python scripts\sampling_sweep.py logs\evaluation\youtube_replay_real_llm_full.json
```

- Chạy cả 6 config trong `config/sampling_sweep.yaml`.
- Ghi lại bảng so sánh + `logs/evaluation/sweep/sweep_summary.json`.
- Nếu 1 config lỗi (server timeout…): chạy lại riêng nó bằng `--only <tên>`.

### B2 — Lọc bằng số

Từ bảng, loại config nào: `assistant_register` hoặc `meta_leak` > baseline; `avg_words`
ngoài [6,20]; `turn_latency_p95` tệ hơn baseline >25%; `fallback` tăng. Giữ 2–3 config
có `distinct_2` cao nhất + `exact_repetition` thấp.

### B3 — Đọc review sample (bắt buộc, số không thay được)

Với mỗi config còn lại, mở report `logs/evaluation/sweep/report_<tên>.json`, đọc
`operator_review_sample` (~30 câu). Đánh giá: tự nhiên/khẩu ngữ hay cứng? có nhạt/lặp
mô-típ ("hehe", "ừ nhỉ" lặp lại)? có rớt giọng trợ lý (giải thích, xin lỗi thừa)?
Ghi nhận xét ngắn từng config.

### B4 — Chốt config + patch

- Chọn config tốt nhất theo B2 + B3 (không chỉ theo điểm auto).
- Patch: chạy lại có `--apply` để tự ghi vào `config/models.yaml`, HOẶC sửa tay
  `llm_main` theo giá trị config đó trong `sampling_sweep.yaml`.

```powershell
python scripts\sampling_sweep.py logs\evaluation\youtube_replay_real_llm_full.json --apply --only <winner> baseline
```

### B5 — Sinh hội thoại mẫu để user nghe

```powershell
python scripts\sample_conversation.py --out logs\evaluation\sample_convo.txt
```

- Chạy qua pipeline production (llama.cpp + filter + fallback, history multi-turn).
- **DỪNG Ở ĐÂY. Trình `sample_convo.txt` cho user, không tự phát hành.**

## Checkpoint người dùng (không bỏ qua)

Trình cho user: (a) bảng số sweep, (b) nhận xét review từng config, (c) `sample_convo.txt`.
Hỏi user duyệt config mới hay muốn thử tay giá trị khác. Chỉ sang bước phát hành khi user OK.

## Phát hành (chỉ sau khi user duyệt) — theo §8

1. Bump `config/system.yaml::app.version` → `1.2.1` (patch: chỉnh sampling, tương thích ngược).
2. Thêm entry `CHANGELOG.md`: giá trị sampling cũ → mới + lý do + số liệu sweep (distinct_2,
   exact_rep, latency trước/sau).
3. Cập nhật `docs/05_CONFIGURATION.md` nếu có mô tả giá trị sampling.
4. KHÔNG cần đổi schema/contract. Rollback = revert giá trị `llm_main` cũ.
5. Chạy lại `sample_conversation.py` một lần cuối làm evidence, đính kèm.

## Báo cáo lại cho user

- Bảng sweep đầy đủ (mọi config).
- Config thắng + lý do (số + nhận xét tai).
- Giá trị sampling cũ → mới (diff `models.yaml`).
- `sample_convo.txt` trước và sau (nếu chạy được baseline để so).
- Version đã bump + dòng CHANGELOG.

## Không làm

- Không fine-tune (đây chỉ là tune sampling).
- Không đổi `inject_mood_directive`, TTS, hay bất kỳ config ngoài `llm_main` sampling.
- Không tin điểm auto-pick một cách mù quáng — luôn đọc output thật.
- Không commit khi test/đánh giá chưa xong.
