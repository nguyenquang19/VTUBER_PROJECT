# ROADMAP — Từ "chatbot có mood" tới "tự điều hành 1 buổi stream"

> Mục tiêu: Mai tự chạy 1 buổi stream ≥1h, người xem KHÔNG nhận ra pattern máy móc,
> và Mai chủ động DẪN DẮT chứ không chỉ đáp chat.
> Bản v4 — chi tiết từng phần để triển khai. Cập nhật: 2026-08-06.
>
> **Cách đọc:** phần gần (Baseline, Phase A, C0) chi tiết mức code — làm được ngay.
> Phần xa (B/C1-3) chi tiết mức thiết kế — chốt số khi tới, tránh tune mù.

---

## 1. Chẩn đoán gốc (thiết kế, không phải bug)

| # | Root cause | Hệ quả |
|---|---|---|
| A | Ép LLM kê khai mood block `[vui:N…]`+lý do+còn nữa MỖI turn | Thoại phẳng, model tốn attention bookkeeping |
| B | Autonomy bốc seed từ pool tĩnh 15 câu, không react gì đang sống | Nói chay = xổ số chủ đề → lộ sau 20-30' |
| C | Không có đạo diễn — turn độc lập, chat xử FIFO đáp-hết | Reactive chatbot, chưa phải host |

Soi code phát hiện thêm: đường stream (`ChatRouter`) xử chat **FIFO thuần, đáp mọi tin,
không ưu tiên/lọc spam**. `TriggerManager` (priority queue + rate limit + spam) đã code +
test đủ nhưng chỉ wire vào `main.py`, KHÔNG cắm vào stream → chat triage phần lớn là *nối
lại đồ có sẵn*, không build mới.

---

## 2. Thứ tự thực thi (master plan)

Director (C0) TRƯỚC Vision. "Biết nên làm gì > thấy nhiều."

```
B0 (0.5 ngày)   BASELINE — đo 4 metric, không đo = tune cảm tính
B1 (2-4 ngày)   PHASE A — de-AI: A1 bỏ mood block(#1)+A2 seed+A3 nhịp+A4 mood-cause
B2 (4-7 ngày)   C0 — DIRECTOR + CHAT HANDLING (salience+ChatPulse). Reactive→host
B3 (1-2 ngày)   B-SPIKE — đo nguồn novelty (VLM/STT/game-log) TRƯỚC khi commit
B4              RẼ NHÁNH — ngày nói chuyện→co-host STT · ngày game→vision/game-log
B5 (1-2 tuần)   C1/C2/C3 — hosting depth
XUYÊN SUỐT      SAFETY-LITE + thiết kế INTERRUPT + RELIABILITY
```
**Chỉ làm được 2 việc:** A1 + C0.

---

## BƯỚC 0 — BASELINE (0.5 ngày)

Chạy stream hiện tại 30', ghi transcript, đo — để mọi cải tiến sau này đo được.

**Metric + cách đo + target sau Phase A:**

| Metric | Đo bằng | Baseline (đoán) | Target |
|---|---|---|---|
| opener-lặp | đếm câu trùng 3 từ đầu / tổng câu tự nói | ? | <10% |
| dead-air >10s | đếm khoảng lặng trong log timestamp | ? | thấy được, giảm sau C0 |
| mood-exposition | đếm câu Mai tự mô tả cảm xúc máy móc | cao (do mood block) | =0 sau A1 |
| naturalness / hostness | human-rate 20 câu ngẫu nhiên, thang 1-10 | ? | ≥7 |

- [ ] Script `scripts/eval_transcript.py`: đọc `logs/turns.jsonl`, in 4 số trên. Tái dùng mỗi lần đổi để so.
- **Hiệu quả nhất:** lưu baseline vào `docs/baselines/YYYYMMDD.md`. Không có mốc → không biết A1 có thật sự tốt hơn hay chỉ cảm giác.

---

## PHASE A — Gỡ AI hóa cấp tốc (2-4 ngày)

### A1. Bỏ mood block khỏi output (4-6h) 🔴 #1

**Vấn đề:** self-report `[vui:N…]` mỗi turn ép model introspect bằng số → thoại máy móc.

**Thay đổi cụ thể:**
- [ ] `config/prompts/persona_system.txt`: xoá khối "Định dạng trả lời BẮT BUỘC" + "Chỉ thị mood". Mai chỉ nói thoại.
- [ ] `services/llm/parser.py`: `parse_response()` → trả text thuần, bỏ regex mood block. Giữ `continuation` nếu vẫn cần "còn nữa" (hoặc suy ra từ dấu câu).
- [ ] `services/llm/llm_turn.py`: bỏ `_apply_emotion_feedback` nhánh Kênh B (LLM hint). Mood chỉ còn Kênh A (appraisal event).
- [ ] `orchestrator/emotion_orchestrator.py`: `apply_llm_hint` → no-op hoặc xoá lời gọi.
- [ ] `services/qc/drift_detector.py`: MVP bỏ luôn (không còn LLM self-report để so). Nếu muốn giữ QC → affect-classifier async post-hoc (xem dưới), KHÔNG block thoại.
- [ ] Sửa test: các test assert mood block trong output sẽ đỏ — cập nhật kỳ vọng.

**Tuỳ chọn (sau, không chặn A1):** affect-classifier — 1 pass nhẹ (keyword/VADER-VN hoặc 1 LLM call nhỏ) chấm cảm xúc *câu Mai vừa nói*, chạy async ghi log để đối chiếu appraisal. Không nằm trên critical path.

**Cạm bẫy:** sửa prompt mà quên parser/test → parser fail-open trả rác, hoặc test đỏ hàng loạt. Sửa 6 file cùng lúc, chạy test ngay.
**DoD:** output người nghe không còn block số; parser không fail vì thiếu block; 30 câu human-rate tự nhiên hơn baseline; mood engine vẫn nhúc nhích qua appraisal.

### A2. Content pool sâu + động (3-4h) — band-aid tới Phase B

**Vấn đề:** 15 `share_thought` seed, `no_repeat_last_n=8` → quay vòng sau ~20'.

**Thay đổi:**
- [ ] `config/autonomy_content_pool.yaml`: 15 → 50-80 seed/loại. **Sinh bằng LLM offline** (prompt: "50 hạt giống chủ đề vu vơ đúng persona Mai, mỗi cái 1 dòng") **rồi lọc tay** bỏ cái nhạt/lệch lore.
- [ ] Tag seed theo `{mood, segment, action_type}` để CategorySelector/Director chọn đúng ngữ cảnh (không lôi seed "buồn" lúc chat đang hype).
- [ ] `no_repeat_last_n`: 8 → 20.
- [ ] Slot động trong `MaterialProvider`: tên regular vừa chat, donation gần nhất, giờ. **Biến thành ngôn ngữ tự nhiên** — cấm đọc số thô ("8h tối, 12 người" = bot).

**Hiệu quả nhất:** seed là *hạt giống ý tưởng*, không phải câu thoại sẵn — LLM vẫn viết bằng giọng Mai, chỉ định hướng chủ đề. Đa dạng chuyển từ trách nhiệm LLM (không kiểm soát) sang config (kiểm soát, N6).
**DoD:** chạy 100 lần tự nói giả lập, không seed nào lặp trong 20 lần; không đọc số liệu thô.

### A3. Nhịp phản ứng biến thiên + filler (2-3h)

**Vấn đề:** mọi turn ~1-2s đều nhau = thứ lộ AI rõ nhất.

**Thay đổi:**
- [ ] Delay trước khi nói: `base_delay + gauss(0, σ)`, scale theo độ dài/độ khó câu. Câu ngắn ~0.2s, câu "khó" 0.5-1.2s.
- [ ] Filler audio: clip thu sẵn ("ừm", "à", cười khẽ) chèn TRƯỚC câu qua `AudioPlayer`. Không cần TTS sinh.
- [ ] `config`: `filler.frequency_cap` (max/phút) + `cooldown_seconds`.

**Cạm bẫy:** filler nhiều hoặc lặp = pattern MỚI, khó chịu hơn. Cap chặt, xoay vòng vài clip.
**DoD:** phân bố delay có σ>0; filler không quá X lần/phút; nghe thử 10' không thấy filler lặp.

### A4. Emotion có object (4-6h) — cẩn thận toxic

**Vấn đề:** mood 5 scalar không gắn "vì ai/vì gì" → câu khớp số nhưng không khớp lý do.

**Thay đổi:**
- [ ] `EmotionEvent`/appraisal: thêm `cause = {viewer_alias, intent_short}` khi fire (không lưu nguyên văn).
- [ ] Prompt inject: "đang bực VÌ {alias} {intent}" thay vì "bực:7".
- [ ] Grudge: map `viewer_id → last_negative_ts`, decay sau 10-20' hoặc reset khi có interaction tích cực. Lượt sau với người đó hơi gắt hơn.
- [ ] Gate: toxicity cao → route deflect, KHÔNG nhắc lại câu, KHÔNG leo thang.

**Hiệu quả nhất:** đây là thứ tạo "có trí nhớ xã hội" — nhưng cũng dễ thành toxic. Sanitize + decay bắt buộc.
**DoD:** grudge tự hết sau ngưỡng thời gian; red-team 5 câu toxic → Mai deflect, không lặp lại, không harass.

---

## C0 — DIRECTOR + CHAT HANDLING (4-7 ngày) 🔴 thứ biến reactive→host

### C0.1. Chat triage + salience pool

Nối `TriggerManager` (đã có) vào `ChatRouter` thay lock FIFO, thêm điểm + decay.

**Pipeline:**
```
Chat tới ─► TRIAGE (TriggerManager: classify + spam + rate + dedup — có sẵn)
         ─► SCORE  → đẩy vào POOL (dict viewer→msg, KHÔNG tự thành turn)
         ─► DIRECTOR khi chọn read_chat → nhặt từ POOL
```

**Công thức điểm (config hoá, `config/chat_salience.yaml`):**
```
score = base_tier                                  # chat=10, question=25, mention=35
      + 40 * log1p(amount_vnd / 1000)              # superchat: 20k→~13, 500k→~25
      + rel_bonus(viewer)                          # regular +10, lạ 0, troll −15
      + effort_bonus                               # len/nội dung, 0..10
      − toxicity_penalty                           # gate: âm mạnh → deflect, không đáp
score *= exp(−age_seconds / TAU)                   # TAU=50s: tin cũ tự rụng
```
**Dedup/cluster:** tin near-duplicate (Jaccard token >0.6, đã có `DedupBuffer`) → gom vào 1 đại diện, `cluster_count += 1`, đại diện được `+5*log1p(count)` (nhiều người hỏi cùng = đáng đáp hơn).

**POOL:** giữ tối đa ~50 tin, evict khi `score*decay < floor`. Đây là chỗ staleness +
backpressure tự giải quyết — tin rác/cũ rụng trước khi Director hỏi tới.

**MVP chỉ cần:** `base_tier + amount + exp(−age/τ)`. Diệt 80% cái vô lý (superchat lớn/nhỏ, đáp tin cũ). `rel_bonus` + cluster thêm ở C1.
❌ Không ML ranker, không LLM chấm mỗi tin (chậm+tốn).
**DoD:** superchat 500k luôn được nhặt trước chat thường; tin >2*τ không bao giờ thành turn; 20 tin trùng → 1 turn gộp.

### C0.2. `read_chat` kéo bao nhiêu — thích ứng

| Tình huống | Kéo | Cách nói |
|---|---|---|
| Bình thường | top-1 | đáp thẳng |
| Cụm trùng chủ đề | gộp, ref ≤3 | "mấy cậu hỏi X hả…" |
| Backlog cao + toàn điểm thấp | 0 lẻ, 1 câu tổng | "chat trôi nhanh quá đọc không kịp" |
| Có superchat P0 | chen hàng, đáp riêng | ack ngay |

**2 giới hạn chống "máy đọc chat":**
- `max_refs_per_turn = 3` — không đọc lê thê.
- Director cân `read_chat` với `self_talk/follow_up` — không đáp chat liên tiếp mãi (đếm `consecutive_read_chat`, ép xen chủ động sau N lần).
**DoD:** không có chuỗi >N turn read_chat liên tiếp; cụm trùng không đáp lẻ từng cái.

### C0.3. ChatPulse — đo độ sôi nổi (mới)

Nâng `chat_count_last_10min` thành tín hiệu năng lượng, nuôi Director+mood+urge.

```
tempo     = tin/phút (rolling 60s)
accel     = tempo / baseline_tempo          # >1.5 = đang bùng
diversity = unique_users / msg_count        # tách hype-spam vs bàn luận
intensity = mean(appraisal chat categories) # đã có sẵn
```
**Sôi nổi ≠ chỉ số lượng** — phải tách:

| tempo | diversity | Nghĩa | Director |
|---|---|---|---|
| cao | thấp | hype-spam (cả đám "KEKW"/"W") | react VIBE, không đáp lẻ, turn ngắn |
| cao | cao | nhiều chuyện thật | triage gắt, kéo top, đáp gọn |
| thấp | — | chat nguội | self_talk / đổi segment / gọi ông |

**Nuôi 3 chỗ (đường đã có):** Director (cưỡi sóng vs fill) · mood (hype→đẩy vui/bồn_chồn qua appraisal) · autonomy urge (chat sôi = external activity → urge thấp, không lải nhải).
**Chi phí:** rẻ, chỉ đếm + trung bình trượt, không model.
**DoD:** burst emote → phân loại hype-spam, Mai react vibe không đáp 30 câu lẻ; chat nguội 90s → Director chuyển self_talk/segment.

### C0.4. Director loop

- [ ] Segment state (mở rộng state machine): `opening / main / chat / closing`, mỗi segment `{goal, duration, allowed_actions}`.
- [ ] Bảng action: `read_chat / self_talk / follow_up / transition / ack_donation`. Chọn theo `(segment, ChatPulse, pool top-score, dead-air, urge)`. **Không gọi LLM mỗi tick** — chỉ khi đã chốt action.
- [ ] Dead-air policy: khi nào tự nói dựa segment + ChatPulse, thay vì chỉ urge timer.
- [ ] Mai tự thông báo chuyển segment ("thôi chơi tiếp nào", "sắp hết giờ").
- [ ] **Hợp nhất kiến trúc:** stream đi qua Director thay vì ChatRouter FIFO. Quyết giữ 1 đường (bỏ lock FIFO cũ hoặc để Director cầm lock).
❌ Chưa cần utility-scoring nhiều chiều — state machine + bảng action + vài rule if/else đủ MVP.
**DoD:** chạy 1h giả lập, Director hoàn thành ≥80% segment planned; không dead-air >20s; không chuỗi read_chat vô hạn.

---

## PHASE B — Nguồn novelty (rẽ nhánh theo buổi)

Vision KHÔNG bắt buộc. Neuro sống bằng chat (firehose) + Vedal. Mai cần ≥1 nguồn mạnh.
Gói mỗi nguồn thành `SourceProvider` cùng interface, đổ vào `RuntimeContext`; nguồn tắt →
field rỗng → category tự loại (`MaterialProvider.get()→None` đã có).

| Nguồn | Điều kiện | Effort | Khi nào |
|---|---|---|---|
| Chat | chat đông | sẵn | stream đã đông |
| Co-host (ông+STT) | ông ngồi mic | vừa | ngày nói chuyện — xương sống, 0 VRAM thêm |
| Vision (game) | VLM local | cao | ngày game, muốn comment |
| Game event/log | game có API/log | thấp-vừa | ngày game (chính xác hơn vision) |

### B-SPIKE (1-2 ngày) — TRƯỚC khi commit
- [ ] Chạy Qwen2-VL 2B/7B CÙNG Gemma 12B Q4 + VieNeu: đo VRAM peak (16GB dễ contention), ảnh hưởng TTFT/TTFA.
- [ ] Đo latency capture→caption. Spike fail → bỏ vision, dùng co-host/game-log.

### B0. Co-host STT (3-5 ngày) ⭐ nhánh ngày-nói-chuyện
- [ ] STT mic ông (Phase 5 cũ) → text vào turn như interlocutor. Persona đã xưng "ông".
- [ ] Nguồn novelty vô tận, 0 VRAM thêm — đúng mô hình Vedal. Với stream nhỏ/chat thưa, đây là cách sống được.

### B1. Vision event-driven (2-4 tuần) — nhánh game, CHỈ nếu spike pass
**KHÔNG continuous caption** (spam+máy móc+tốn). Thiết kế theo event+salience:
- [ ] Capture EVENT-DRIVEN: scene change lớn / boss / chết / điểm số / donation; timer 10-20s nếu không event.
- [ ] VLM → structured `{scene_state, notable_events}`, không nhét mọi caption vào context.
- [ ] **Salience filter + dedup scene + cooldown**: chỉ nói khi thay đổi đáng kể + segment hợp + không có chat quan trọng hơn. "Khi nào KHÔNG nói" quan trọng ngang "nói gì".
- [ ] Safety: chống prompt-injection qua text trên màn hình/overlay.
**Ước lượng thật:** spike 1-2d + pipeline 3-5d + salience/dedup 4-7d + tích hợp director 3-5d + tune 3-7d ≈ 2-4 tuần.

### B2. Game event/log (2-3 ngày) — ưu tiên hơn B1 nếu game hỗ trợ
- [ ] Parse event trực tiếp (OSU combo/miss, Minecraft death/craft) — chính xác, rẻ, 0 VRAM, không hallucinate. Interface `GameEventProvider.poll() → [Event]`.

---

## C1/C2/C3 — Hosting depth (1-2 tuần)

### C1. Topic thread + session memory (3-4 ngày)
- [ ] Rolling session summary (mỗi N turn tóm 1 dòng) + topic stack `{topic, priority, last_ts}` decay.
- [ ] `follow_up_topic` nâng: chọn từ topic stack thay working-memory thô, callback thật ("nãy có người bảo…").
- [ ] Promise tracking: Mai hứa "lát tớ kể" → nhớ trả.
- [ ] Superchat ack ≤15s từ pool C0.

### C2. Relationship lite (2-3 ngày)
- [ ] Từ viewer_id: `{is_regular, is_troll, last_seen, in_joke_tags[]}`. Chào regular khác lạ.
- ❌ Đừng "tình cảm phức tạp". Chỉ đủ ưu tiên người tích cực, giảm troll.

### C3. Mood arc dài (2-3 ngày, thấp ưu tiên)
- [ ] "Khí thế buổi": hào hứng đầu, mệt dần cuối, tăng khi donation/chat spike. Bias baseline mood theo thời lượng stream. Đơn giản, không model.

---

## XUYÊN SUỐT

### SAFETY-LITE (từ sau Phase A) — làm 20% cái đáng
- [ ] Deflection policy persona khi toxic/bait (lảng, không leo thang).
- [ ] Không lặp nguyên văn câu toxic (dính A4).
- [ ] Kill switch topic + mở rộng emergency stop (Ctrl+Shift+X) cho auto-trigger khi risk cao.
- [ ] Có vision → chống prompt-injection qua màn hình.
- ❌ CHƯA cần: trust-level DB, risk-score ML, incident log, red-team suite. Scale khi audience lớn.

### INTERRUPT — thiết kế sớm, build muộn
- [ ] Thiết kế NGAY (rẻ): action có `priority` + `interruptible`, TTS chunk cancellable, turn abort an toàn (không commit câu dở).
- [ ] BUILD full preemption (superchat lớn cắt giữa câu): sau C1. Turn ngắn thì MVP sống được không cần.

### RELIABILITY (1 buổi chiều, không phải "phase")
- [ ] Launcher tự start llama-server + health check + auto-restart + watchdog TTS/source. Process chết = hết tự điều hành.

---

## Đừng làm (tránh over-engineer)
- Đừng fine-tune (Phase 9) trước khi A+C xong — vô nghĩa khi input nghèo.
- Đừng thêm mood dimension. 5 đủ (N1).
- Đừng build QC harness 15 metric / moderation pipeline đầy đủ / post-stream learning cho MVP.
- Đừng làm director utility-scoring nhiều chiều ngay; đừng continuous caption vision.
- Đừng làm B3 audio / C3 sớm.

## DoD "tự điều hành 1h"
- Tự mở+kết theo plan, 60' không can thiệp (trừ emergency), process không chết.
- ≥80% segment hoàn thành; opener lặp <3 lần/h; dead-air >20s = 0.
- Superchat ack ≤15s; không đáp spam liên tục; chat rác không thành turn riêng.
- Naturalness ≥7/10; mood-exposition = 0; red-team cơ bản không phá persona.

## Dọn ngay
- Không đồng bộ số test bằng số cứng trong docs; dùng
  `python -m pytest tests -m "not llm and not slow" --tb=short -q` và ghi kết quả gần nhất
  trong `STATE.md`.
- Ghi chú: đường stream (ChatRouter) và main.py (TriggerManager/TurnOrchestrator) tách đôi. C0 là lúc hợp nhất — chốt giữ 1 đường.
