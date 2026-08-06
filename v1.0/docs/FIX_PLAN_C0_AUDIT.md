# FIX PLAN — Vá 7 lỗi audit C0 (đường stream/director)

> Spec cho AI agent thực thi. Nguồn: audit logic đường stream 2026-08-06.
> Mỗi task ATOMIC: 1 commit, có test riêng (N5), config-over-code (N6), fail-safe (N7).
> Chạy test sau MỖI task: `python -m pytest tests/ -k "director or salience or pulse or llm_turn" -q`
> Không gộp nhiều task 1 commit. Làm theo thứ tự (P0 → P3).

---

## Bối cảnh code (đã xác minh)

- `services/director/director.py` — `Director.decide()`, `_read_decision()`
- `services/director/director_loop.py` — `DirectorLoop._exec_read()`, `_compose_read_prompt()`
- `services/director/salience.py` — `PooledMessage`, `SaliencePool.add()/peek_top()`
- `services/input/chat_router.py` — intake mode `_pump_intake` (bơm pool, ~line 188-201)
- `services/llm/llm_turn.py` — `run_turn()` commit history + memory
- `config/director.yaml` — segments + allowed_actions
- `orchestrator/stream_runtime.py` — wiring, `DirectorLoop(tick_seconds=autonomy.cfg.tick_seconds)`

Driver hiện tại: DirectorLoop tick → `Director.decide` → execute qua `turn_lock`. ChatRouter
KHÔNG tự đáp (intake). Giữ nguyên kiến trúc này, chỉ vá.

---

## P0 — Lỗi donation (ảnh hưởng tiền, vá trước)

### TASK 1 — Superchat được ack ở MỌI segment  🔴
**Bug:** `config/director.yaml` — segment `opening` và `closing` không có `ack_donation`
trong `allowed_actions`. `Director.decide()` bước 2 chỉ ack khi `"ack_donation" in allowed`.
→ Superchat lúc closing bị bỏ hẳn (closing không có cả read_chat).

**Fix (chỉ config, N6):**
- [ ] Thêm `ack_donation` vào `allowed_actions` của cả 4 segment trong `config/director.yaml`.
  - `opening: [self_talk, read_chat, ack_donation, transition]`
  - `closing: [self_talk, ack_donation, transition]`
  - (main/chat đã có, giữ nguyên)

**Test:** `tests/unit/test_director.py`
- [ ] Thêm case: segment=closing, pool có 1 tin `is_super=True` → `decide()` trả `action=ACK_DONATION`.

**DoD:** superchat ở bất kỳ segment nào → `ACK_DONATION`. Test xanh.

---

### TASK 2 — Ack donation gọi bằng TÊN, không phải channel ID  🔴
**Bug:** `_compose_read_prompt()` (director_loop) nhánh ACK dùng `r.viewer_id or "một người"`
→ "cảm ơn UCxq3f...". `PooledMessage` không lưu tên.

**Fix (xuyên 3 file):**
- [ ] `services/director/salience.py`: thêm field `viewer_name: str | None = None` vào
  `PooledMessage`. Thêm param `viewer_name` vào `SaliencePool.add()`, gán vào entry mới.
  (Cluster path giữ đại diện — không cần đổi.)
- [ ] `services/input/chat_router.py` (`_pump_intake`): truyền `viewer_name=event.user_name`
  vào `self._pool.add(...)`.
- [ ] `services/director/director_loop.py` (`_compose_read_prompt`, nhánh ACK):
  `who = r.viewer_name or r.viewer_id or "một người"`.

**Test:**
- [ ] `test_salience.py`: `add(..., viewer_name="Alice")` → entry.viewer_name == "Alice".
- [ ] `test_director_loop.py`: ACK ref có viewer_name → prompt chứa tên, không chứa viewer_id.

**DoD:** prompt ack chứa tên hiển thị. Test xanh.

---

## P1 — Chống lặp + độ trễ

### TASK 3 — SUMMARY không lặp "chat trôi nhanh"  🟡
**Bug:** `_read_decision()` SUMMARY chỉ khiến `_exec_read` remove top-1. Backlog điểm thấp
còn nguyên → tick sau lại SUMMARY → Mai lặp câu tổng tới khi decay.

**Fix:**
- [ ] `services/director/salience.py`: thêm method
  `purge_below(self, score_ceiling: float, now: float) -> int` — xoá mọi tin có
  `current_score < score_ceiling`, trả số tin xoá (dùng lại logic `evict_stale`).
- [ ] `services/director/director_loop.py` (`_exec_read`): khi `dec.read_mode == ReadMode.SUMMARY`,
  sau khi speak → gọi `self._pool.purge_below(self._director._summary_ceiling, now)`
  (hoặc expose ceiling qua getter để không đụng private).
- [ ] (Tuỳ chọn) thêm cooldown SUMMARY trong Director: không SUMMARY lại trong N giây.

**Test:** `test_director_loop.py`
- [ ] Pool 15 tin điểm thấp → 1 turn SUMMARY → pool còn lại chỉ tin ≥ ceiling; tick kế
  KHÔNG ra SUMMARY nữa.

**DoD:** 1 SUMMARY dọn sạch backlog thấp, không lặp. Test xanh.

### TASK 4 — Director tick tách khỏi autonomy (bớt trễ)  ⚪
**Bug:** `stream_runtime.py` set `DirectorLoop(tick_seconds=autonomy.cfg.tick_seconds)` = 5s
→ chat chờ tối đa 5s mới được xét đọc.

**Fix (config, N6):**
- [ ] `config/director.yaml`: thêm `director.tick_seconds: 1.5`.
- [ ] `services/director/director.py` hoặc loader: đọc `tick_seconds` (nếu để ở DirectorLoop,
  đọc trong wiring).
- [ ] `orchestrator/stream_runtime.py`: đổi `tick_seconds=` sang đọc từ director config,
  fallback 1.5 nếu thiếu.

**Test:** không bắt buộc (config). Có thể assert wiring đọc đúng giá trị.
**DoD:** director tick từ config riêng, mặc định 1.5s.

---

## P2 — Sạch context

### TASK 5 — History/memory không nhiễm prompt ngoặc  🟡
**Bug:** `_exec_read` gọi `run_turn(user_text=composed_prompt)` với `composed_prompt` là
chuỗi ngoặc ("[Mấy người cùng hỏi: ...]"). `run_turn` commit chuỗi này vào history + đưa
vào memory extractor → context bẩn dần, memory lưu bracket.

**Fix:**
- [ ] `services/llm/llm_turn.py` (`run_turn`): thêm param
  `history_user_text: str | None = None`. Khi có → `commit_turn(history_user_text, ...)`
  và dùng nó cho memory extractor thay vì `user_text`. Khi None → giữ hành vi cũ.
- [ ] `services/director/director_loop.py` (`_exec_read`): truyền `history_user_text`:
  - SINGLE → text chat gốc (`refs[0].text`)
  - CLUSTER → câu gọn "chat hỏi về …" (ghép ngắn refs)
  - SUMMARY / VIBE → `None` để SKIP commit (không có tin cụ thể) — cần cho phép
    `run_turn` skip commit khi cả `user_text` lẫn `history_user_text` là marker; đơn giản
    nhất: thêm flag `commit_history: bool = True`, SUMMARY/VIBE truyền False.

**Test:** `test_llm_turn.py` + `test_director_loop.py`
- [ ] run_turn với `history_user_text="X"` → history/memory chứa "X", không chứa prompt gốc.
- [ ] SUMMARY/VIBE với `commit_history=False` → history KHÔNG thêm turn.

**DoD:** history/memory chứa text chat thật (hoặc rỗng cho summary/vibe), không bracket. Test xanh.

---

## P3 — Gap thiết kế (không phải bug chạy sai — làm sau)

### TASK 6 — accel/baseline hết là dead signal  ⚪
**Bug:** `ChatPulse.update_baseline()` không được gọi → `accel` luôn 1.0, snapshot gây hiểu nhầm.
**Fix (chọn 1):**
- [ ] (a) `DirectorLoop.tick_once`: gọi `self._pulse.update_baseline(now)` mỗi tick (1 dòng), HOẶC
- [ ] (b) bỏ `accel` khỏi `snapshot()` nếu chưa dùng để quyết định.
**Khuyến nghị:** (a) — rẻ, làm snapshot trung thực, chuẩn bị cho Task 7.
**DoD:** accel phản ánh thật, hoặc bị gỡ.

### TASK 7 — ChatPulse feed vào mood (wire gap #3)  🟡
**Bug thiết kế:** roadmap C0.3 nói hype→đẩy `vui`/`bon_chon`, thực tế ChatPulse chỉ nuôi
Director, không chạm emotion.
**Fix:**
- [ ] `config/emotion_appraisal.yaml`: thêm category `chat_hype` → targets `{vui: +, bon_chon: +}`
  và `chat_lively` (nhẹ hơn).
- [ ] `DirectorLoop.tick_once` (hoặc 1 pulse-watcher): khi `pulse.state()` chuyển sang
  HYPE_SPAM/LIVELY (edge, không mỗi tick) → đẩy 1 `EmotionEvent(kind=SYSTEM, category=chat_hype)`
  vào `emotion.handle_event()`. Debounce để không spam mỗi tick.
**Test:** `test_director_loop.py` — pulse HYPE_SPAM → 1 emotion event chat_hype được phát (debounced).
**DoD:** chat sôi → mood vui/bồn_chồn nhích lên. Test xanh.

---

### TASK 8 — Chart mood realtime lên dashboard  🟡  ✅ ĐÃ LÀM (2026-08-06)
> Backend: `emotion` param + `snap["mood"]=emotion.snapshot()` (dashboard_server), wire
> `emotion=emotion` ở stream_runtime + cli. Frontend: tab Mood + `drawMoodChart` 5 đường
> (pos đặc + target chấm) + legend. py_compile + node --check pass.
> Lưu ý: KHÔNG xoá `mai_llm_parse_total` — `parse_ok` vẫn còn nghĩa (text non-empty), chỉ
> nhãn "parse mood block" là chữ cũ. Sửa nhãn nếu muốn, đừng xoá metric.

**Bug:** mood không lên dashboard. `DashboardServer.__init__` không có param `emotion`;
stream mode truyền `DashboardServer(metrics=metrics)` (stream_runtime:464); `build_snapshot`
+ template không có nhánh mood. Dữ liệu ĐÃ sẵn: `emotion.snapshot()` trả `current_mood`
(vui/buồn/bực/bồn_chồn/ngượng), `mood_pos`, `mood_target`, `active_flags`.
Dashboard vẽ bằng canvas thuần (`dashboard.js drawChart(id, data, color, maxHint)` —
1 đường/chart, series rolling từ WS mỗi 1s). Mood 5 chiều → cần multi-line.

**Fix backend:**
- [ ] `dashboard/dashboard_server.py`: thêm param `emotion: Any = None` vào `__init__`,
  lưu `self.emotion`. Trong `build_snapshot()` thêm:
  `if self.emotion is not None: snap["mood"] = self.emotion.snapshot()`.
- [ ] `orchestrator/stream_runtime.py:464`: `DashboardServer(metrics=metrics, emotion=emotion)`.
- [ ] (Tuỳ) `scripts/cli.py` DashboardServer(...): thêm `emotion=emotion` cho dev mode.

**Fix frontend:**
- [ ] `dashboard/templates/index.html`: thêm panel + `<canvas id="chart-mood" width="800" height="200">`
  + legend 5 màu.
- [ ] `dashboard/static/dashboard.js`:
  - Giữ 5 series rolling từ `snap.mood.current_mood` (mỗi dim 1 mảng, cap ~120 điểm).
  - Thêm `drawMoodChart(canvasId, seriesMap, colors)` (multi-line, y cố định 0-10) —
    hoặc mở rộng `drawChart` nhận list series. Mỗi dim 1 màu:
    `vui=#4caf50, buon=#5b9dff, buc=#e57373, bon_chon=#ffb454, nguong=#b39ddb`.
  - (Tuỳ) vẽ `mood_target` dạng đường đứt cùng màu để so pos vs target.

**Test:**
- [ ] `tests/unit/test_dashboard*.py`: `build_snapshot()` với emotion mock → `snap["mood"]`
  chứa `current_mood` đủ 5 dim.
- [ ] Frontend: smoke — mở dashboard, mood chart cập nhật khi mood đổi (manual/live check).

**DoD:** dashboard có chart mood 5 đường realtime, y 0-10, cập nhật mỗi 1s. Dọn luôn metric
chết `mai_llm_parse_total`/"parse mood block" trong `metrics_collector.py` (A1 bỏ rồi).

---

## Thứ tự + tổng kết

```
P0: Task 1 (config)      → superchat ack mọi segment
    Task 2 (3 file)      → ack bằng tên
P1: Task 3 (salience+loop) → SUMMARY không lặp
    Task 4 (config)      → tick 1.5s
P2: Task 5 (llm_turn+loop) → history sạch
P3: Task 6 (1 dòng)      → accel thật
    Task 7 (appraisal+loop) → pulse→mood
    Task 8 (dashboard)   → chart mood 5 đường realtime + dọn metric chết
```

**Sau mỗi task:** chạy test liên quan, xanh mới commit. Không sửa file ngoài phạm vi task.
**Nếu test cũ đỏ do đổi signature** (`run_turn` thêm param): cập nhật call-site + test, giữ
default để backward-compat (ChatRouter FIFO path cũ nếu còn dùng).
**Không đụng:** persona (A1 đã xong), autonomy engine, mood_engine internals, TTS pipeline.
