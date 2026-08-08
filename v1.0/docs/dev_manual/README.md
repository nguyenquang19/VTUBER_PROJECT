# Mai — Developer Manual (canonical)

> Bộ tài liệu kỹ thuật DUY NHẤT của dự án. **AI onboarding: đọc file này TRƯỚC.**
> Phản ánh code THẬT tại 2026-08-07. Nếu doc lệch code → **code đúng**, sửa doc.
> ⚠️ Codebase tiến rất nhanh (nhiều session/agent). Bảng dưới verify ngày 08-07;
> luôn đọc code + `STATE.md` để chốt, đừng tin doc suông.

Mai = AI VTuber tiếng Việt, chạy 100% local (Windows 11 / RTX 5060 Ti 16GB). Đọc chat
YouTube+Discord → Director quyết nói gì → LLM (Gemma 4 12B Q4) sinh câu → TTS (VieNeu) phát.
Có mood engine, memory, autonomy tự nói, và pipeline thu data fine-tune đang chạy.

---

## 1. ĐÃ XÂY TỚI ĐÂU (verify 2026-08-07)

**Chú thích:** ✅ xong & wired · 🟡 xong nhưng có nợ · ⬜ chưa động.

| Subsystem | TT | Ghi chú |
|---|---|---|
| Foundation (config/log/eventbus/state/fallback/metrics) | ✅ | Có test |
| LLM stack + **A1 (de-AI)** | ✅ | Bỏ mood block khỏi output (verify `raw_had_mood_block=false`) |
| **Sampling de-AI** | ✅ | payload có `min_p/top_p/top_k/repeat_penalty/presence_penalty` (models.yaml) |
| **Mood → giọng nói (`mood_style`)** | ✅ | `mood_style.yaml` + `services/emotion/mood_style.py`; `_format_mood_context` bỏ số thô `vui=N`+event_category, bơm chỉ dẫn chữ (thai_do/nhip/do_dai/tu_dem) |
| Emotion / Mood engine (spring-damper + appraisal) | ✅ | Mood 1 chiều: sự kiện→engine→prompt→giọng. DriftDetector gỡ |
| Autonomy v2 | ✅ | Ở stream là **generator** cho Director (`force_generate`) |
| **M1 Agent State + Event Ledger** | ✅ | Shared grounded state, bounded/dedup/TTL, dashboard snapshot; context toggle OFF |
| **C0 Director** (Salience+ChatPulse+Director+DirectorLoop) | 🟡 | Built+wired, thay FIFO. **7 lỗi** → `FIX_PLAN_C0_AUDIT.md` (Task 8 xong) |
| Chat salience / triage | 🟡 | Điểm+decay+cluster chạy; **chưa có cổng chống spam** ở intake |
| TTS (VieNeu streaming) | ✅ | Giọng ưng nhưng **phẳng 1 tông** — chưa prosody theo mood |
| Platform I/O (YouTube/Discord/intake) | ✅ | **Chưa verify live E2E với viewer thật** |
| Memory (sqlite-vec/bge-m3/extractor) | 🟡 | Extractor regex; **P95 bge-m3 live chưa đo** |
| Dashboard (tab **Mood** + **Review**) | ✅ | Mood chart 5 chiều realtime; Review = chấm điểm + **sửa trực tiếp câu Mai** |
| **Phase 8 — Data pipeline fine-tune** | ✅ | ĐANG THU DATA LIVE (xem mục 2) |
| Reliability supervisor (auto-restart/watchdog) | ⬜ | Chạy tay 2 terminal |
| Avatar (Phase 6) | ⬜ | Chưa có gì |
| STT (P5) / **Fine-tune (P9)** | ⬜ | P9 chờ đủ data + input chốt |

**Test suite:** không ghi số cứng vì tăng theo milestone. Lấy trạng thái hiện tại bằng
`python -m pytest tests -m "not llm and not slow" --tb=short -q`; xem `STATE.md` cho
kết quả regression gần nhất.

**Mức độ hoàn thiện (honest):**
- **Cái "não" (LLM+chat+mood+giọng+director+autonomy+data): ~85%** — register đã de-AI, data pipeline chạy.
- **Sản phẩm "lên sóng được": ~55-60%** — vẫn chặn bởi avatar (0%), reliability (~30%), **chưa chạy live thật**.

---

## 2. Phase 8 — Data pipeline (đang chạy, cho AI biết để KHÔNG phá)

Thu data để fine-tune Mai v2 sau. ĐÃ LIVE:
- **`turns.jsonl`** làm giàu (schema_version 2, context_block, mood_state, cause, filter_verdict,
  was_regen, persona_version) → SFT pairs.
- **`pref_pairs.jsonl`**: mỗi lần regen (filter/dedup) → cặp DPO (chosen>rejected) miễn phí.
- **`ratings.jsonl`** (`/api/rate` 👍👎🚩) + **`corrections.jsonl`** (`/api/correct`, tab Review sửa
  trực tiếp — data mạnh nhất: target SFT + cặp DPO gốc→sửa).
- PII: `services/data/sanitize.py` hash viewer_id + mask email/phone/token.
- Export: `scripts/export_dataset.py` → `sft_*.jsonl` + `dpo_*.jsonl` (lọc canned/bad/blocked).
- **CHƯA fine-tune** (P9): chờ ~2-5k turn good + input ổn định.

Sửa gì đụng LLM turn / dashboard → giữ nguyên các sink data này (fail-safe, không giết turn).

---

## 3. Đọc theo thứ tự

| File | Đọc khi nào |
|---|---|
| **01_architecture.md** | Tổng thể: layer, data flow 1 turn (chat + tự nói), driver Director |
| **02_modules.md** | Chi tiết module (LLM/TTS/Memory/Emotion/Autonomy/**Director** §12/Platform) |
| **03_operations.md** | Chạy, config (yaml), dashboard, test, troubleshoot |
| **04_extending.md** | Thêm module/nguồn mới, **2 đường điều phối** (stream vs legacy) |

## 4. Tài liệu khác trong `docs/`

| File | Vai trò |
|---|---|
| `CLAUDE.md` | Rules N1-N8 — đọc trước khi sửa code |
| `persona.md` | Persona A/B/C (phần "Format output" là LỊCH SỬ — A1 đã bỏ mood block) |
| `ROADMAP_AUTONOMOUS_HOST.md` | Kế hoạch "tự điều hành 1 buổi stream" |
| `FIX_PLAN_C0_AUDIT.md` | 🟡 Vá lỗi C0 (Task 8 xong; **Task 1-7 CÒN CHỜ**) |
| `PLAN_MOOD_STYLE.md` | ✅ ĐÃ IMPLEMENT (mood→giọng) — giữ làm tham chiếu thiết kế |
| `PLAN_FINETUNE_DATA.md` | ✅ ĐÃ IMPLEMENT (Phase 8) — giữ làm tham chiếu thiết kế |
| `STATE.md` (root) | Ledger "đang ở đâu" theo thời gian |

## 5. Việc đang mở (thứ tự đề xuất cho AI)

1. **M2 GoalManager + Agenda policy** — chỉ bắt đầu sau khi M1 được user review.
2. **Vá C0** — `FIX_PLAN_C0_AUDIT.md` P0 (superchat ack Task 1-2 ra tiền; SUMMARY/history…).
3. **Tune** — chỉnh chữ `mood_style.yaml` + số sampling từ transcript thật (hot-reload, không restart).
4. **Reliability supervisor** — launcher auto-restart + watchdog (để chạy 1h không người).
5. **Avatar/Fine-tune** — làm sau lõi và khi dữ liệu đủ gate.

## 6. Convention + lưu ý

- **Đường dẫn:** relative `v1.0/`.
- **Chốt cứng:** Python 3.11, torch 2.11+cu128, Gemma 4 12B Q4, VieNeu-TTS v3 Turbo.
- **2 đường điều phối (DỄ NHẦM):** stream đi qua `DirectorLoop` (services/director). Legacy
  (`main.py`/`cli.py` không-director) dùng `TriggerManager`/`TurnOrchestrator`/`StateMachine`.
  Sửa hành vi stream → sửa `services/director/`, KHÔNG sửa TriggerManager. (§9b của 04)
- **Update doc khi:** đổi logic module, thêm/bỏ thành phần, hoặc phát hiện doc sai.

## 7. Lịch sử doc

Spec cũ (ARCHITECTURE, EMOTION_SIMULATION, AUTONOMY_ENGINE_REDESIGN, MOOD_SYSTEM, PROCESS,
QUICKSTART, SYSTEM_OVERVIEW, phase0_report) **đã xoá 2026-08-06** khi chuẩn hoá về dev_manual.
Nội dung đúng gộp vào 01–04. Dev_manual là nguồn duy nhất.
