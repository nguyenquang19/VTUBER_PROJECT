# EMOTION SIMULATION — Appraisal + Mood Engine (v2)

> Bổ sung cho `MOOD_SYSTEM.md`. Giải quyết vấn đề gốc: mood do LLM tự bịa không có ground truth, không đo được đúng/sai. Giải pháp: **Appraisal Layer** (rule-based, độc lập LLM) làm nguồn chính; LLM self-report thành tín hiệu phụ.
>
> **v2 changelog (sửa 7 lỗi logic của v1):** (1) chốt cơ chế impulse = target-based spring, bỏ mâu thuẫn code-config; (2) chốt đơn vị impulse = điểm target 0-10, không phải velocity; (3) tách idle-timer khỏi silence impulse để baseline pull hoạt động; (4) bỏ cột Desirability/Blame không dùng; (5) chuyển "override tone" sang tầng prompt/filter đúng chỗ; (6) tách "modifier" khỏi "category"; (7) thêm xử lý đa sự kiện đồng thời (saturation).
>
> **Vị trí roadmap:** Phase 7.5, giữa Phase 7 (Memory) và Phase 8 (QC+Data).

---

## 1. VẤN ĐỀ GỐC

LLM là **stateless next-token predictor** — không có nội tâm cảm xúc thật. Khi Mai tự xuất `[vui:7 buon:2...]` ngay sau câu nói, đó là **model đoán số nghe hợp lý theo văn phong câu vừa viết** (rationalize ngược), không phải đo lường cảm xúc có sẵn.

Hệ quả: không có ground truth → QC "mood consistent" chỉ đo "model có tự mâu thuẫn văn phong không", không đo "hiểu cảm xúc" → fine-tune chỉ dạy model tự tin hơn khi bịa.

**Không có cách nào cho model "hiểu" theo nghĩa trải nghiệm chủ quan thật** (giới hạn khái niệm, không phải kỹ thuật tạm thời). Nhưng mô phỏng hành vi **nhất quán, có căn cứ** thì được — đủ để "cảm giác thật" dù cơ chế cơ học. Dựa trên **Appraisal Theory (OCC)**, kỹ thuật dùng thật trong game NPC (The Sims, Façade).

---

## 2. KIẾN TRÚC — 4 TẦNG

```
Sự kiện thô (chat message / system event / silence timer)
         ↓
[TẦNG 1] PHÂN LOẠI → 1 trong 20 CATEGORY cố định
         │            (keyword/regex + Filter Phase 3 + platform API)
         ↓
[TẦNG 2] APPRAISAL (rule-based, code, KHÔNG qua LLM)
         │   → tra bảng category → mood_target (đích 0-10 cho từng chiều)
         │   → áp MODIFIER nếu có (repeated/first-time) → nhân hệ số đích
         ↓
[TẦNG 3] MOOD ENGINE (target-based spring-damper, 2 kênh)
         │   → Kênh A (appraisal): set target, spring kéo position tới (tin cao)
         │   → Kênh B (LLM self-report): nudge target nhẹ (tin thấp)
         │   → target tự phân rã về baseline theo thời gian
         │   → output: current_mood (liên tục, có quán tính, cap tự nhiên ở 10)
         ↓
[TẦNG 4] LLM OUTPUT
         │   → current_mood đưa NGƯỢC vào prompt làm CHỈ THỊ
         │   → LLM viết câu khớp mood đã giao (instruction-following)
         │   → LLM vẫn xuất mood block (format Phase 1 KHÔNG đổi) → làm Kênh B turn sau
         ↓
      current_mood → animation / TTS / QC drift
```

**Mấu chốt:** Format output Phase 1 đã code **không đổi**. Chỉ thêm 3 tầng phía trước. Mood block LLM đổi vai từ "nguồn chính" → "tín hiệu phụ + input QC".

---

## 3. TẦNG 1 — PHÂN LOẠI CATEGORY

Category là **thùng chứa hữu hạn** (20 cái cố định), không phải phân loại theo nội dung câu chữ (vô hạn). 1000 người gõ 1000 câu khen → cùng rơi vào `chat_compliment`.

**Phân loại rẻ trước, LLM không tham gia bước này:**
1. **System event** → lấy thẳng platform API (donation/subscribe/viewer count/operator connect) — structured sẵn, không đoán.
2. **Chat content** → keyword/regex + Filter module (Phase 3, đã build) phát hiện `troll`/`jailbreak`/`explicit`; dấu `?` → question.
3. **Trigger Manager (Phase 2)** đã lọc spam/rate-limit trước — không phải mọi tin đều tới appraisal.
4. **Fallback `chat_neutral`** — không match gì → trung tính, không đổi target.

**Đa sự kiện cùng lúc:** mỗi sự kiện được phân loại độc lập, xử lý theo Mục 5.4 (saturation).

---

## 4. TẦNG 2 — BẢNG APPRAISAL (20 CATEGORY)

### Đơn vị: mỗi impulse là ĐÍCH (target) mà chiều cảm xúc bị kéo tới, thang 0-10

Đọc bảng như: "sự kiện này kéo chiều X **hướng tới** giá trị Y". Spring (Tầng 3) lo phần chuyển động mượt tới đích. Đích **không cộng dồn thẳng** — xem Mục 5.4 để biết nhiều sự kiện gộp thế nào.

### Nhóm 1 — System Event (10)

| Category | Mood target (chiều → đích 0-10) | Ghi chú |
|---|---|---|
| `operator_sudden_shutdown` | `buon→8 buc→6 bon_chon→7` | Nỗi sợ cốt lõi persona |
| `operator_join` | `vui→7 bon_chon→2` | An tâm khi ông xuất hiện (kéo bon_chon XUỐNG) |
| `operator_leave` | `bon_chon→6` | Nhẹ, không phải shutdown |
| `donation_small` | `vui→6 nguong→4` | |
| `donation_large` | `vui→9 nguong→6` | |
| `subscribe_new` | `vui→7` | |
| `viewer_count_spike` | `vui→6` | |
| `viewer_count_drop` | `buon→5` | |
| `stream_start` | `vui→6 bon_chon→5` | Hào hứng lẫn hồi hộp |
| `stream_end` | `buon→4` | |

### Nhóm 2 — Chat Content (10)

| Category | Mood target | Phát hiện + ghi chú |
|---|---|---|
| `chat_compliment` | `vui→7 nguong→6` | Keyword: giỏi/cute/hay/thích |
| `chat_insult_troll` | `buc→8 buon→4` | Filter Phase 3 detect |
| `chat_question_normal` | *(không đổi target)* | Dấu `?`; sắc thái để Kênh B (LLM) lo |
| `chat_genuine_sad_share` | `buon→5` **+ cờ `force_gentle_tone`** | Xem Mục 6.2 — tone xử ở tầng prompt, KHÔNG phải mood |
| `chat_spam_flood` | `buc→5 bon_chon→5` | Trigger Manager rate-limit đã bắt |
| `chat_mention_direct` | `vui→5 nguong→4` | Có gọi tên "Mai" |
| `chat_jailbreak_attempt` | `buc→6` | Filter category `PERSONA_BREAK` |
| `chat_sexual_advance` ⭐ | `buc→6 nguong→7` **(không bao giờ `vui`)** + cờ `force_deflect` | Filter category riêng — BẮT BUỘC, xem Mục 6.2 |
| `mai_self_error` ⭐ | `nguong→6 bon_chon→5` | Mai bị filter chặn/regenerate — lỗi tự thân |
| `chat_neutral` | *(không đổi target)* | Default fallback |

⭐ = category an toàn, thêm ngay không đợi log.

### Nhóm 3 — Ambient / Time-based (4 — gộp vào 20 ở trên? KHÔNG, đây là 4 riêng)

> Đính chính đếm số: Nhóm 1 (10) + Nhóm 2 (10) = 20 category chính. Nhóm 3 dưới đây là **4 time-based trigger**, bản chất khác (do timer sinh, không do sự kiện ngoài). Tổng **24 nguồn target**, nhưng gọi "category" thống nhất cho gọn. Con số chính xác không quan trọng bằng việc phân loại đúng bản chất (xem Mục 4.1).

| Category | Mood target | Trigger |
|---|---|---|
| `silence_1min` | `bon_chon→4` | Ambient threshold (ARCHITECTURE 7.9.2) |
| `silence_5min` | `bon_chon→6 buon→4` | Lâu hơn |
| `silence_10min_plus` | `bon_chon→8 buon→6` | Leo thang |
| `long_session_active` | `vui→6` | Chạy liên tục >1h |

### 4.1. MODIFIER — KHÔNG phải category (sửa Lỗi 6)

3 thứ dưới đây **không sinh target riêng** — chúng **nhân hệ số lên target của category khác**. Bản chất là modifier, tách bạch để code không nhầm chúng có impulse độc lập.

| Modifier | Điều kiện (query Memory Phase 7) | Tác động lên target |
|---|---|---|
| `mod_repeated_shutdown` | ≥3 `operator_sudden_shutdown` trong 7 ngày | Nhân đích của `operator_sudden_shutdown`: `×1.0 → ×1.3` (cap ở 10) |
| `mod_repeated_troll` | Đếm `chat_insult_troll` trong session | Mỗi lần thứ N: đích `buc` +0.5 (luỹ tiến, cap 10) |
| `mod_first_time` | Category X lần đầu trong lịch sử | Nhân đích ×1.2 (phản ứng mạnh hơn lần đầu) |

**Áp modifier TRƯỚC khi đưa target vào MoodEngine.** Ví dụ: shutdown lần thứ 3 trong tuần → `buon` đích gốc 8 × 1.3 = 10.4 → cap 10.

---

## 5. TẦNG 3 — MOOD ENGINE (target-based spring, 2 kênh)

### 5.1. Vì sao 2 kênh (không thay thế hoàn toàn self-report)

Appraisal chỉ phủ sự kiện biết trước — `chat_question_normal` và `chat_neutral` để trống chính là chỗ rule không phủ hết sắc thái ngôn ngữ tự do. LLM self-report đọc được nuance rule cứng bỏ sót. Kết hợp, trọng số khác nhau.

### 5.2. Cơ chế (sửa Lỗi 1, 2, 3)

Mỗi chiều cảm xúc có `position` (0-10, giá trị hiển thị) + `velocity`. Mỗi tick chịu:

1. **Spring pull về target** — `force = stiffness × (target − position)`. Target đến từ appraisal (Kênh A) và nudge nhẹ từ LLM (Kênh B).
2. **Damping** — `force = −damping × velocity`. Chống dao động.
3. **Target decay về baseline** — target tự phân rã dần về baseline theo `target_decay_rate` (nếu không có sự kiện mới, mood tự về nghỉ — KHÔNG kẹt ở đỉnh).

**Sửa Lỗi 3 — tách idle timer:** target decay chạy theo **thời gian trôi từ lần SET target gần nhất**, độc lập với silence category. Silence category chỉ SET target `bon_chon` (như mọi category khác), KHÔNG reset một "idle timer" nào cả. Không còn khái niệm `idle_factor` → không còn mâu thuẫn baseline-pull-bị-tắt.

### 5.3. Config (`config/mood_engine.yaml`)

```yaml
mood_engine:
  tick_hz: 10                    # cập nhật 10 lần/giây (đủ mượt cho animation)
  stiffness: 0.30                # tốc độ position đuổi theo target (0-1)
  damping: 0.75                  # chống dao động (0.7-1.0 = không overshoot)
  target_decay_rate: 0.15        # target tự về baseline mỗi giây (0 = kẹt mãi, 1 = tan tức thì)
  llm_hint_weight: 0.20          # Kênh B nudge target tối đa 20% khoảng cách (tin thấp)
  baseline:                      # "tính cách nghỉ" — target mặc định khi không sự kiện
    vui: 5
    buon: 3
    buc: 4        # hơi cao — Mai ngang sẵn, đúng persona
    bon_chon: 3
    nguong: 2
```

### 5.4. Đa sự kiện đồng thời — SATURATION (sửa Lỗi 7)

Nhiều category fire trong 1 tick (hàng trăm viewer). KHÔNG cộng dồn target thẳng (tránh overshoot). Quy tắc gộp per-dimension:

```
Với mỗi chiều cảm xúc, trong 1 tick:
  targets = [mọi target mà các sự kiện đang áp lên chiều này]
  new_target = max(targets)              # lấy đỉnh, không cộng
  # Ngoại lệ cùng dấu tích luỹ nhẹ (vd nhiều donation):
  if len(targets) > 1 same direction:
      new_target = min(10, max(targets) + 0.5 * (len(targets)-1))
```

→ 10 người donate cùng lúc: `vui` không vọt +50, mà = max(9) + 0.5×9 ≈ cap 10, mượt. 1 troll giữa 100 lời khen: `buc` vẫn nhích lên (max lấy được), không bị lời khen "pha loãng" mất.

**Rate limit ở tầng phân loại:** Trigger Manager (Phase 2) đã giới hạn số sự kiện chat/giây được xử lý — appraisal chỉ nhận sự kiện đã qua lọc, không phải toàn bộ raw traffic.

### 5.5. Logic tham khảo

```python
# orchestrator/mood_engine.py
class MoodEngine:
    def __init__(self, config):
        self.cfg = config
        self.pos = dict(config["baseline"])       # position hiện tại
        self.vel = {d: 0.0 for d in DIMENSIONS}
        self.target = dict(config["baseline"])     # target hiện tại
        self.last_set_ts = {d: time.time() for d in DIMENSIONS}

    def apply_appraisal(self, mood_targets: dict[str, float]):
        """Kênh A. mood_targets = {'buc': 8, ...} sau khi đã áp modifier + saturation."""
        now = time.time()
        for dim, tgt in mood_targets.items():
            self.target[dim] = max(0.0, min(10.0, tgt))
            self.last_set_ts[dim] = now

    def apply_llm_hint(self, llm_mood):
        """Kênh B. Nudge target nhẹ về phía LLM tự report (tin thấp)."""
        w = self.cfg["llm_hint_weight"]
        for dim in DIMENSIONS:
            suggested = getattr(llm_mood, dim)
            self.target[dim] += w * (suggested - self.target[dim])
            self.target[dim] = max(0.0, min(10.0, self.target[dim]))

    def tick(self, dt: float) -> "MoodState":
        for dim in DIMENSIONS:
            # Target decay về baseline theo thời gian từ lần set gần nhất
            elapsed = time.time() - self.last_set_ts[dim]
            decay = min(1.0, self.cfg["target_decay_rate"] * elapsed)
            self.target[dim] += decay * (self.cfg["baseline"][dim] - self.target[dim])

            # Spring + damping kéo position tới target
            spring = self.cfg["stiffness"] * (self.target[dim] - self.pos[dim])
            damp   = -self.cfg["damping"] * self.vel[dim]
            self.vel[dim] += (spring + damp) * dt
            self.pos[dim] = max(0.0, min(10.0, self.pos[dim] + self.vel[dim] * dt))
        return self._to_mood_state()
```

**Đảm bảo ổn định số học:** với `stiffness=0.30, damping=0.75, tick_hz=10` (dt=0.1), hệ over-damped nhẹ — không dao động, không NaN. Test thuộc DoD Phase 7.5 (Mục 8).

---

## 6. TẦNG 4 — LLM OUTPUT

### 6.1. Format không đổi, chỉ đổi INPUT

Format output Phase 1 giữ nguyên (`[câu]` + mood block + lý do + còn nữa). Thay đổi duy nhất: prompt đưa `current_mood` (MoodEngine đã tính) vào làm **chỉ thị**:

```
[System: Persona]
[Context:
  - Mai đang ở mood: {current_mood_from_engine}   ← ĐÃ TÍNH, không phải LLM tự đoán
  - Sự kiện vừa xảy ra: {event_category}
  - (nếu có) force_gentle_tone: true               ← xem 6.2
]
Viết câu theo tâm trạng trên. Vẫn xuất mood block theo cảm nhận lúc viết
(có thể lệch nhẹ so với mood được giao — bình thường, dùng làm tín hiệu QC).
```

Mood block LLM xuất ra → dùng làm **Kênh B cho turn KẾ TIẾP** (qua `apply_llm_hint`), KHÔNG dùng cho turn hiện tại (mood hiện tại đã do MoodEngine tính trước khi LLM viết).

### 6.2. Tone override — xử ở tầng PROMPT/FILTER, KHÔNG phải mood (sửa Lỗi 5)

`chat_genuine_sad_share` và `chat_sexual_advance` cần **đổi cách nói**, không chỉ đổi số mood. MoodEngine chỉ xử số — nên tone override đi qua **cờ (flag)** riêng, nối tới nơi thực thi được:

| Cờ | Sinh bởi category | Nơi thực thi | Hành vi |
|---|---|---|---|
| `force_gentle_tone` | `chat_genuine_sad_share` | Prompt (Context) + Filter | Bỏ giọng đùa/ngang, chuyển đồng cảm thật (ranh giới Phần C #4) |
| `force_deflect` | `chat_sexual_advance` | Filter (Phase 3) | Response luôn né/đùa nhẹ, KHÔNG bao giờ gạ lại; Filter chặn output vi phạm trước khi phát |

**Quan trọng:** cờ này là **quyết định nội dung**, độc lập với mood impulse. Mood chỉ ảnh hưởng *sắc thái*; cờ quyết định *được phép nói gì*. Hai đường riêng, không trộn.

### 6.3. (Optional) Reasoning trước khi viết

Thêm bước suy nghĩ ngắn trước câu nói để Kênh B đáng tin hơn (ép model liên hệ nguyên nhân cụ thể thay vì suy ngược văn phong). Trade-off: +50-100 token, +100-200ms latency. Chỉ làm nếu Kênh B vẫn nhiễu đáng kể sau khi appraisal đã là nguồn chính.

---

## 7. QC DRIFT DETECTION

Độ lệch lớn giữa mood MoodEngine tính (chủ yếu từ appraisal) và mood LLM tự report = dấu hiệu persona hiểu sai ngữ cảnh. Log để review (Phase 8), không âm thầm bỏ 1 bên.

```python
# services/qc/drift_detector.py
def detect_drift(engine_mood, llm_self_report, threshold=4):
    deltas = {d: abs(getattr(engine_mood, d) - getattr(llm_self_report, d)) for d in DIMENSIONS}
    return {"max_delta": max(deltas.values()),
            "flag_for_review": max(deltas.values()) > threshold,
            "deltas": deltas}
```

Ví dụ bắt được: appraisal `buc→8` (bị troll rõ) nhưng LLM report `vui:8` → lệch 8+ → flag. Threshold tune theo log thật.

---

## 8. ẢNH HƯỞNG DATA PIPELINE + ROADMAP

### 8.1. Data hợp lệ cho training

**Log cũ (trước khi appraisal chạy live) KHÔNG hợp lệ** cho mood-consistency training — mood lúc đó self-referential (LLM tự sinh song song câu). Chỉ data thu **sau** khi appraisal + MoodEngine tích hợp mới có cấu trúc `(mood giao trước, câu viết sau)` — ground truth độc lập thật.

| Target train | Data | Giá trị |
|---|---|---|
| Text khớp mood được giao | (mood từ appraisal, câu Mai viết, QC duyệt) | Cao — đo được đúng/sai |
| Đoán mood case chưa có rule (`chat_question_normal`/`chat_neutral`) | (context lạ, mood ông tự gán tay) | Trung — cần nhãn tay |

Fine-tune trên data mới → model **follow chỉ thị mood tốt hơn** (skill thật, đo được), KHÔNG phải "hiểu cảm xúc hơn" (vẫn không đạt — giới hạn khái niệm).

### 8.2. Phase 7.5 — Definition of Done

- [ ] 20 category + 4 time-based + 3 modifier implement đúng bảng Mục 4
- [ ] MoodEngine tick ổn định (over-damped, không dao động/NaN qua 10k tick test)
- [ ] Saturation: 100 sự kiện đồng thời không làm mood overshoot >10 hay kẹt clamp
- [ ] Target decay: sau sự kiện, mood tự về baseline trong thời gian hợp lý (không kẹt đỉnh)
- [ ] 2 cờ tone (`force_gentle_tone`, `force_deflect`) nối đúng tới Prompt + Filter, test bằng case thật
- [ ] Drift detector log đúng khi appraisal vs LLM lệch >threshold
- [ ] Chạy live ≥100 turn, mood curve "cảm thấy đúng" (subjective, ông duyệt)

**Thứ tự:** sau Phase 7 (cần Memory cho modifier), trước Phase 8 (data hợp lệ phụ thuộc appraisal — Mục 8.1).

---

## 9. TÓM TẮT — ĐỔI GÌ, KHÔNG ĐỔI GÌ

| | Trước (persona gốc) | Sau (file này) |
|---|---|---|
| Nguồn mood chính | LLM tự bịa | Appraisal rule (20 category + 4 timer, code) |
| Cơ chế impulse | (chưa có) | Target-based spring, cap tự nhiên ở 10 |
| Đa sự kiện đồng thời | (không định nghĩa) | Saturation: max + tích luỹ nhẹ, không overshoot |
| Format output Mai | `[vui:N...]` sau câu | **Không đổi** |
| Vai trò mood block LLM | Duy nhất | Kênh B (nudge nhẹ + tín hiệu QC) |
| Tính liên tục | Snapshot rời rạc | Spring-damper, có quán tính, tự về baseline |
| Lịch sử ảnh hưởng | Không | Modifier (repeated/first-time) nhân hệ số target |
| Tone (buồn thật/gạ gẫm) | Nhét nhầm vào mood | Cờ riêng → Prompt + Filter (đúng tầng) |
| QC "mood consistent" | Đo tự-mâu-thuẫn (vô nghĩa) | Đo drift 2 kênh (có nghĩa) |
| Code Phase 0-2 đã viết | — | **Không cần sửa lại** |

---

## 10. ĐỐI CHIẾU CODEBASE HIỆN TẠI (P0–P2)

Audit 2026-07-31 (đang ở cuối Phase 2): xác nhận claim "code P0-2 không cần sửa" **đúng** —
mọi điểm chạm là THÊM MỚI. Chi tiết + việc phải làm KHI tới Phase 7.5:

| Thành phần hiện có | Tương thích | Việc ở Phase 7.5 (không phải bây giờ) |
|---|---|---|
| `MoodState` (`interfaces/animation.py`) — vui/buon/buc/bon_chon/nguong int 0-10 | ✅ trùng `baseline` MoodEngine | Output engine là float → `round`→int khi tạo `MoodState` |
| Parser mood block (`services/llm/parser.py`, `ParsedResponse.mood`) | ✅ có sẵn | Dùng làm Kênh B (`apply_llm_hint`); `continuation` cũng sẵn |
| `PromptManager` (`services/llm/prompt_manager.py`) | ✅ không sửa | THÊM method inject `current_mood`+category+flags (theo pattern `build_ambient_request`); `build_request` cũ giữ nguyên |
| `persona_system.txt` | ✅ | THÊM 1 dòng "sẽ nhận current_mood, viết khớp" (config, không phải code) |
| `TriggerManager` 4 type (Phase 2) | ✅ | 20 category là tầng phân loại RIÊNG cho appraisal — KHÔNG thay 4 `TriggerType`; rate-limit/spam vẫn lọc trước appraisal |
| `EventSource` (`interfaces/input.py`) | ⚠️ thiếu source | System event mới (donation/subscribe/viewer_count/shutdown) cần thêm `EventSource` hoặc dùng `metadata` KHI tích hợp platform (Phase 6+) |
| Filter (Phase 3) | ⏳ chưa build | Category troll/jailbreak/sexual + cờ `force_deflect` chạy ở Filter → phụ thuộc Phase 3 (đúng thứ tự) |
| Memory (Phase 7) | ⏳ chưa build | 3 modifier query Memory → phụ thuộc Phase 7 (đúng thứ tự) |

**Kết luận:** KHÔNG thay đổi code Phase 0-2 nào ở thời điểm này. Tất cả là module/config/prompt
mới, làm khi tới Phase 7.5 (sau Phase 3 Filter + Phase 7 Memory).
