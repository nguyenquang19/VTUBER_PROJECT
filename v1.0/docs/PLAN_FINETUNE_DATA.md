# PLAN — Lưu data cho fine-tune (Phase 8 data pipeline)

> Spec cho AI agent. Mục tiêu: thu thập + lưu data chất lượng NGAY BÂY GIỜ để sau fine-tune
> Mai v2 (SFT + DPO). Data không log = mất vĩnh viễn → bắt đầu sớm, kể cả trước khi train.
> Nguyên tắc: log giàu nhưng rẻ (append JSONL, không block turn), sanitize PII, versioned schema.
> Fail-safe (N7): lỗi ghi log KHÔNG được giết turn. 1 task = 1 commit + test.

---

## 0. Ta cần data gì để fine-tune 1 persona VTuber

| Loại | Dùng cho | Nguồn |
|---|---|---|
| **SFT pair** (input context → câu Mai) | Dạy model NÓI như Mai | mỗi turn tốt |
| **Preference pair** (rejected vs chosen) | DPO — dạy model tránh câu dở | mỗi lần **regen** (filter/dedup) → bản đầu = rejected, bản sau = chosen |
| **Quality label** (good/bad/flag) | Lọc data + tín hiệu DPO | operator chấm live (mạnh nhất) + fallback level + filter verdict |
| **Metadata** | Lọc/split/debug | mood, cause, event, trigger, session, timestamp |

Điểm vàng: hệ **đã có sẵn** 2 nguồn regen (FilterRegenerator + dedup ambient) → mỗi lần regen là 1 cặp DPO cho không. Chỉ cần bắt cả 2 bản.

---

## TASK 1 — Làm giàu turn log (SFT record đủ field)

Hiện `LLMTurnRunner._log_turn` (services/llm/llm_turn.py) ghi `turns.jsonl` với:
`turn_id, kind, user_text, mai_text, raw_had_mood_block, parse_ok, mood_dominant,
mood_intensity, trigger_type, level_used, latency_ms, viewer_id, session_id`.

**Thiếu để train được.** Thêm các field (fail-safe: field nào lấy lỗi → null, không raise):

```jsonc
{
  "schema_version": 2,              // versioning — bắt buộc, để export parse đúng
  "turn_id": 42, "ts": "2026-08-06T20:15:03+07:00",
  "session_id": "sess_1", "kind": "chat_reply",   // chat_reply | ambient | director_read | transition

  // ---- INPUT (đủ để rebuild training example) ----
  "persona_version": "a1b2c3d4",   // hash PromptCache (persona_system.txt) — KHÔNG lưu full persona mỗi turn
  "context_block": "- đang thiên về 'bực' VÌ... \n- Đang khá bực: cộc, gắt...",  // system message mood/cause/directive đã render
  "mood_state": {"vui":3,"buon":2,"buc":9,"bon_chon":4,"nguong":1},
  "mood_cause": {"alias":"viewer_7","intent":"hỏi đểu"},   // A4, đã sanitize
  "event_category": "chat_troll",
  "user_text": "mày ngu à",        // tin chat / null nếu ambient
  "history_len": 8,                 // số message history lúc đó (không cần lưu full mỗi turn)

  // ---- OUTPUT ----
  "mai_text": "Hứ, khôn thì tự đi mà trả lời.",

  // ---- QUALITY SIGNALS ----
  "level_used": 0,                 // 0 primary (train được) · 1 canned (KHÔNG train)
  "filter_verdict": {"passed": true, "categories": [], "regen": false},
  "was_regen": false,              // true nếu output này là bản regen (chosen)
  "raw_had_mood_block": false,     // A1 sanity
  "operator_rating": null,         // "good"|"bad"|"flag"|null — Task 3 điền
  "latency_ms": 1450
}
```

**Không lưu full persona + full history mỗi turn** (bloat): lưu `persona_version` hash +
`history_len`. Export (Task 5) rebuild history từ chuỗi turn cùng session theo thứ tự.

**Sửa:** mở rộng `_log_turn` nhận thêm các field trên (truyền từ `run_turn`/`run_ambient_turn`
đã có sẵn context/mood/verdict). Ghi qua `TurnLogger` hiện tại (append JSONL, rotation).

**Test:** 1 turn giả → record có `schema_version`, `context_block`, `mood_state`, `level_used`.
Turn canned (level 1) → vẫn ghi nhưng `level_used=1`.

---

## TASK 2 — Bắt preference pair khi regen (DPO miễn phí)

2 chỗ regen trong code:
- `services/filter/regenerator.py` (FilterRegenerator) — output xấu → append hint → regen.
- `services/llm/llm_turn.run_ambient_turn` + `director_loop` — dedup hit → regen 1 lần.

**Sửa:** khi regen xảy ra, ghi 1 record vào file RIÊNG `logs/pref_pairs.jsonl`:
```jsonc
{
  "schema_version": 1, "ts": "...", "session_id": "...", "turn_id": 42,
  "prompt_ref": {"persona_version":"...", "context_block":"...", "user_text":"..."},
  "rejected": "câu bản đầu (bị filter chặn / trùng lặp)",
  "chosen":   "câu bản regen (đã pass / khác)",
  "reason": "filter:persona_break" | "dedup:ambient"
}
```
→ Đây là data DPO chuẩn (cùng prompt, chosen > rejected). Không cần gán nhãn tay.

**Test:** giả lập filter chặn 1 lần → regen → `pref_pairs.jsonl` có 1 record với rejected≠chosen.

---

## TASK 3 — Operator chấm điểm live (label mạnh nhất)

Không có nhãn người thì chỉ có tín hiệu yếu. Thêm cách operator gắn nhãn **turn gần nhất**:

- **Dashboard** (dashboard_server.py + dashboard.js): 3 nút `👍 good / 👎 bad / 🚩 flag` →
  `POST /api/rate {rating}` → gắn vào turn cuối (giữ `last_turn_id` trong runtime) → cập nhật
  record trong `turns.jsonl` (hoặc ghi `logs/ratings.jsonl` với turn_id để export join sau —
  đơn giản hơn, không sửa file cũ).
- **Hotkey** (tuỳ chọn, tái dùng infra `keyboard` như emergency stop): F7=good, F8=bad.

Ghi `logs/ratings.jsonl`: `{turn_id, session_id, rating, ts}`. Export chỉ join theo
khóa ghép `(session_id, turn_id)`, vì `turn_id` bắt đầu lại từ 1 ở mỗi phiên.

**Test:** `POST /api/rate` với rating=good → ratings.jsonl có record trỏ đúng turn_id cuối.

---

## TASK 4 — Sanitize PII (làm từ đầu, không sửa sau)

Data sẽ đem train → không được lẫn PII người xem.
- **viewer_id**: hash (sha1 8 char) trước khi ghi, KHÔNG lưu channel id gốc. Map gốc→hash
  giữ riêng `data/viewer_map.db` (không đi kèm dataset).
- **viewer_name / alias**: chỉ lưu ở `mood_cause.alias` dạng đã sanitize (đã có `sanitize_alias`
  trong classifier) — dùng lại. Không lưu tên thật trong `mai_text`/`user_text`? user_text giữ
  nguyên (cần cho training) nhưng export có bước scrub tên nếu lộ.
- Không lưu token/email/số điện thoại nếu regex bắt được trong user_text → mask `[PII]`.

**Test:** ghi turn với viewer_id="UCxxxx" → log chứa hash, KHÔNG chứa "UCxxxx".

---

## TASK 5 — Script export dataset (làm khi đủ data, spec sẵn)

`scripts/export_dataset.py`: đọc `turns.jsonl` + `ratings.jsonl` + `pref_pairs.jsonl` → emit 2 dataset.

**Lọc (bỏ rác):**
- Bỏ `level_used=1` (canned) — không phải giọng Mai thật.
- Bỏ `filter_verdict.passed=false` chưa regen (câu bị chặn).
- Bỏ `operator_rating="bad"`. Ưu tiên cao `operator_rating="good"`.
- Bỏ turn `parse_ok=false` / `mai_text` rỗng.
- Scrub PII lần cuối.

**Output A — SFT** (`data/datasets/sft_YYYYMMDD.jsonl`), format messages:
```jsonc
{"messages":[
  {"role":"system","content":"<persona rút gọn hoặc theo persona_version>"},
  {"role":"system","content":"<context_block: mood directive + cause>"},
  {"role":"user","content":"<user_text>"},          // ambient → user role = chỉ thị tự nói
  {"role":"assistant","content":"<mai_text>"}
]}
```
(Quyết định lúc export: train KÈM persona đầy đủ, hay rút gọn để model tự nội hoá Mai — 2 biến thể.)

**Output B — DPO** (`data/datasets/dpo_YYYYMMDD.jsonl`) từ `pref_pairs.jsonl`:
```jsonc
{"prompt":"<rebuild từ prompt_ref>", "chosen":"<chosen>", "rejected":"<rejected>"}
```

**In thống kê:** tổng turn, %good/bad, số cặp DPO, phân bố mood/kind → biết data đủ chưa.

**Test:** input mẫu 5 turn (2 good, 1 canned, 1 bad, 1 regen) → SFT ra 2 record (bỏ canned+bad),
DPO ra 1 cặp.

---

## TASK 6 — Lưu trữ + retention

- File tại `logs/` (append, rotation theo ngày như logging.yaml hiện có). KHÔNG commit vào git
  (thêm `logs/*.jsonl`, `data/datasets/`, `data/viewer_map.db` vào `.gitignore`).
- Backup định kỳ `logs/*.jsonl` sang `backups/data/` (tái dùng cơ chế backup trước migration).
- `schema_version` trong MỌI record → export xử được nhiều version.

---

## TASK 7 — Operator SỬA TRỰC TIẾP câu Mai (data vàng nhất)

Rating (T3) chỉ nói "dở"; sửa trực tiếp nói "phải nói THẾ NÀY" → SFT target chuẩn nhất +
cặp DPO sạch (gốc=rejected, sửa=chosen). Đây là nguồn data giá trị cao nhất.

**Chế độ A — Dashboard tab "Review" (chính, tái dùng FastAPI+JS đã có):**
- Tab mới liệt kê N turn gần nhất (ring buffer trong runtime hoặc tail `turns.jsonl`).
- Mỗi turn hiển thị `user_text` + `mai_text` trong **textarea sửa được** + nút `Lưu sửa` / `Bỏ qua` / 👍👎.
- `Lưu sửa` → `POST /api/correct {session_id, turn_id, corrected_text}` → ghi
  `logs/corrections.jsonl`:
  ```jsonc
  {"turn_id":42, "session_id":"...", "original":"<mai_text gốc>", "corrected":"<câu operator sửa>", "ts":"..."}
  ```

Record legacy thiếu `session_id` được exporter gán namespace riêng theo nguồn
(`legacy:turns`, `legacy:ratings`, `legacy:corrections`, `legacy:pref_pairs`). Các
namespace này không join chéo; exporter không suy đoán quan hệ chỉ từ `turn_id`.
- **Post-hoc, KHÔNG đụng live** (câu đã nói/đã phát TTS rồi) → an toàn, không rủi ro timing.

**Chế độ B — File-based (fallback siêu đơn giản, không cần UI):**
- `scripts/review_dump.py` xuất turn cần review ra `corrections_todo.jsonl` (mỗi dòng có field
  `corrected: ""`) → operator mở bằng editor bất kỳ, điền `corrected` → re-import.

**Ăn khớp export (T5):** turn có correction → SFT target = `corrected` (thay `mai_text`);
sinh cặp DPO (rejected=original, chosen=corrected). Turn đã sửa = ưu tiên CAO NHẤT trong dataset.

❌ KHÔNG làm sửa-live-inline (sửa lúc TTS đang phát) — phức tạp + rủi ro, post-hoc đủ.

**Test:** `POST /api/correct` → `corrections.jsonl` có record; export ưu tiên `corrected` +
tạo đúng 1 cặp DPO gốc→sửa.

> Ghi chú: hành vi (persona, `mood_style.yaml`, canned responses, seed pool) vốn đã human-editable
> qua config + hot-reload. T7 là để sửa OUTPUT thành data train — khác với sửa config.

---

## Thứ tự + DoD

```
T1 enrich turn log → T2 pref pairs → T3 rating + T7 correction (nhãn người) → T4 PII sanitize
→ (T5 export + T6 retention: làm khi bắt đầu tích data / trước khi train)
```

**DoD:**
- Mỗi turn primary ghi record đủ field train (schema_version=2), fail-safe không giết turn.
- Mỗi regen (filter/dedup) tạo 1 cặp trong `pref_pairs.jsonl`.
- Operator chấm được good/bad/flag, join được theo turn_id.
- **Operator sửa trực tiếp câu Mai → `corrections.jsonl`; export dùng câu sửa làm target + tạo cặp DPO.**
- Không PII thô (viewer_id đã hash) trong log.
- Export script chạy trên data thật → ra sft.jsonl + dpo.jsonl hợp lệ, thống kê in ra.

## Đừng over-engineer (giữ N1)
- ❌ Chưa cần: auto quality scoring bằng model, labeling UI phức tạp, active-learning, DB riêng.
  JSONL + nút thumb + join theo turn_id là đủ cho tới khi có ≥ vài nghìn turn.
- ❌ Chưa fine-tune (Phase 9) cho tới khi: đủ data (target ~2-5k turn good), input đã ổn định
  (mood_style + sampling đã tune xong — train trên input còn đổi là phí).
- ✅ Ưu tiên T1+T2+T4 làm SỚM (bắt data ngay). **T7 (sửa trực tiếp) là data mạnh nhất** —
  làm cùng T3. T5/T6 để sau, chỉ cần spec sẵn.

## Liên quan
- Input phải chốt trước khi train: `PLAN_MOOD_STYLE.md` (mood→giọng) + sampling. Fine-tune trên
  input đang thay đổi = data lệch pha, phải thu lại.
- `context_block` log ở T1 chính là thứ `PLAN_MOOD_STYLE` sinh ra → 2 plan khớp nhau.
