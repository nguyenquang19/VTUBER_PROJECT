# AUTONOMY ENGINE v2 — Thiết kế lại hệ thống tự nói / tự dẫn dắt

> Thay thế: ARCHITECTURE.md Section 7.9.1 (`AMBIENT_TALK`), 7.9.2 (`_should_ambient_talk`/`_create_ambient_trigger`), 7.9.4 (content generation).
> Không đổi: 3 trigger type còn lại (`OPERATOR_VOICE`, `CHAT_MENTION`, `CHAT_NORMAL`), state machine 5 state, interrupt policy 7.9.3.
> Nguyên tắc N1 (YAGNI) vẫn giữ — đây KHÔNG phải thêm 12 trigger type, mà là làm **1 trigger type hiện có** (`ambient_talk`) thông minh hơn, thay vì thêm phình.

---

## 0. Vì sao bản hiện tại thấy "giả", không giống người

Đối chiếu code thật trong ARCHITECTURE 7.9.2:

```python
def _should_ambient_talk(self) -> bool:
    silence = (datetime.now() - self.last_speak_time).seconds
    return silence > 60
```

5 vấn đề cụ thể:

1. **Threshold là hằng số, không có variance.** Con người không im lặng đúng 60.000s rồi bật nói — luôn có dao động. Một con số cứng lặp lại tạo ra nhịp điệu đếm-giờ mà người xem nhận ra rất nhanh (nhất là stream dài, xem vài chục lần là thấy pattern).
2. **Chỉ có 1 "lý do" để nói: hết giờ.** 7.9.4 đưa 1 prompt chung ("càm ràm / kể chuyện / hỏi chat / gọi ông") và để LLM tự chọn ngẫu nhiên trong đầu — không có state nào track đã chọn gì lần trước, nên dễ lặp category, hoặc LLM có xu hướng thiên vị 1 kiểu (model thường lệch về 1-2 pattern quen).
3. **Không có cooldown riêng cho chính hành vi tự nói.** `last_speak_time` dùng chung cho cả Mai tự nói lẫn Mai trả lời người khác → sau khi ambient talk xong, nếu vẫn im lặng, 60s sau lại kích tiếp, đều đặn. Không có "tự nói xong thì nghỉ lâu hơn bình thường" như người thật.
4. **Mood không nối vào đây.** Theo MOOD_SYSTEM.md, mood (kể cả v1 self-report đã có từ Phase 1) hoàn toàn không được đọc bởi trigger manager. Một người bồn chồn thật sẽ sốt ruột lên tiếng sớm hơn người đang thoải mái — hiện tại `bồn_chồn` cao hay thấp không ảnh hưởng gì đến việc có tự nói hay không.
5. **Không có build-up.** Ambient talk hiện tại là step function (0 hoặc 1 tại giây 60), không có giai đoạn "đang nhen nhóm muốn nói" — cái mà bản Phần D persona.md có mô tả định tính ("bồn chồn tăng dần") nhưng chưa hề được code hoá ở tầng trigger.

---

## 1. Nguyên tắc thiết kế v2

- **Tách "có nên nói" khỏi "nói gì".** Hai bộ não riêng: `UrgeAccumulator` (khi nào) và `CategorySelector` (nội dung gì). Đây là lý do chính khiến giờ nó trộn lẫn và cứng.
- **Biến thiên là bắt buộc, không phải optional polish.** Mọi threshold thời gian đều có jitter ngẫu nhiên. Không có hằng số nào lộ ra ở behavior quan sát được từ bên ngoài.
- **Nội dung phải phân loại + có trí nhớ ngắn hạn tránh lặp.** Không cho phép chọn lại category vừa dùng trong N lần gần nhất.
- **Dùng mood NGAY BÂY GIỜ, không chờ Phase 7.5.** Mood v1 self-report (đã có từ Phase 1, format persona.md Phần B) đủ tốt để làm input thô cho `UrgeAccumulator`. Khi Phase 7.5 (Appraisal Engine) xong, chỉ đổi nguồn input, không đổi kiến trúc autonomy.
- **Tự nói phải tự "biết điều".** Nếu tự nói mà chat/operator vẫn im lặng tiếp, tần suất phải giảm dần (không nagging) — giống Neuro-sama không lải nhải liên tục khi chat không phản hồi.
- **Vẫn N6 (config over code).** Toàn bộ số ở `config/autonomy.yaml`, không hardcode.

---

## 2. Kiến trúc

```
                    ┌─────────────────────┐
  mood (v1/v2) ───► │   UrgeAccumulator    │──► urge score (0-100), tick mỗi 5-10s
  silence timer ───►│  (spring, không step)│
  chat activity ───►└─────────┬────────────┘
                               │ vượt threshold động (có jitter)
                               ▼
                    ┌─────────────────────┐
                    │  Xác suất phát trigger│ (Poisson-like, không phải if >X thì luôn phát)
                    └─────────┬────────────┘
                               ▼
                    ┌─────────────────────┐
  recent categories ►│  CategorySelector    │──► chọn 1 trong N category,
  (ring buffer)      │  (weighted, no-repeat)│    loại trừ vừa dùng + cooldown riêng
                    └─────────┬────────────┘
                               ▼
                    ┌─────────────────────┐
                    │ Trigger AMBIENT_TALK │──► TriggerManager (giữ nguyên priority:10)
                    │ + category + mood ctx│
                    └─────────────────────┘
```

### 2.1. `UrgeAccumulator` — thay `_should_ambient_talk`

```python
# orchestrator/autonomy_engine.py

class UrgeAccumulator:
    """
    Tích luỹ 'muốn nói' liên tục thay vì check 1 điểm thời gian.
    Tick mỗi TICK_SECONDS (config), không phải check-on-demand.
    """

    def __init__(self, cfg: AutonomyConfig):
        self.cfg = cfg
        self.urge: float = 0.0                # 0-100
        self.last_external_activity: datetime = datetime.now()
        self.last_self_speak: datetime = datetime.now()
        self.recent_categories: deque[str] = deque(maxlen=cfg.no_repeat_window)
        self.consecutive_ignored: int = 0     # số lần tự nói liên tiếp mà không có phản hồi

    def tick(self, current_mood: MoodState) -> None:
        silence = (datetime.now() - self.last_external_activity).seconds
        self_cooldown_left = self._self_cooldown_remaining()

        if self_cooldown_left > 0:
            # Vừa tự nói xong — urge không được tăng trong giai đoạn nghỉ,
            # mô phỏng "vừa nói xong chưa vội nói tiếp ngay"
            self.urge = max(0.0, self.urge - self.cfg.decay_after_speak)
            return

        # Base rise theo thời gian, KHÔNG tuyến tính — chậm lúc đầu, nhanh dần
        # (giống người: im lặng 10s chưa sốt ruột, im lặng 90s thì có)
        base_rise = self.cfg.rise_curve(silence)

        # Mood modifier: bồn_chồn kéo urge lên nhanh hơn, buồn/ngượng kéo chậm lại
        mood_mult = 1.0
        mood_mult += (current_mood.bon_chon / 10) * self.cfg.bon_chon_weight
        mood_mult -= (current_mood.buon / 10) * self.cfg.buon_dampen
        mood_mult -= (current_mood.nguong / 10) * self.cfg.nguong_dampen
        mood_mult = max(0.2, mood_mult)  # không cho về 0 tuyệt đối

        # Nagging penalty: nói ambient liên tiếp mà không ai phản hồi → giảm dần
        nag_penalty = max(0.4, 1.0 - 0.15 * self.consecutive_ignored)

        noise = random.gauss(0, self.cfg.urge_noise_std)  # biến thiên ngẫu nhiên mỗi tick

        self.urge = clamp(
            self.urge + base_rise * mood_mult * nag_penalty + noise,
            0.0, 100.0
        )

    def should_speak_now(self) -> bool:
        """
        KHÔNG phải if self.urge > threshold: return True (step function).
        Xác suất phát trigger tăng dần theo urge — vùng 'có thể nói bất cứ lúc nào'
        thay vì 1 điểm chính xác, giống Poisson process.
        """
        if self.urge < self.cfg.urge_floor:      # dưới sàn: chắc chắn chưa nói
            return False
        p = self.cfg.probability_curve(self.urge)  # ví dụ sigmoid quanh urge=60-80
        return random.random() < p

    def on_self_spoke(self) -> None:
        self.last_self_speak = datetime.now()
        self.consecutive_ignored += 1
        self.urge = 0.0

    def on_external_activity(self) -> None:
        """Gọi khi operator/chat lên tiếng — reset đúng ý nghĩa, khác last_speak_time cũ."""
        self.last_external_activity = datetime.now()
        self.consecutive_ignored = 0   # có người phản hồi → hết bị coi là "nagging"

    def _self_cooldown_remaining(self) -> int:
        elapsed = (datetime.now() - self.last_self_speak).seconds
        return max(0, self.cfg.self_cooldown_seconds - elapsed)
```

Khác biệt cốt lõi so với bản cũ: **`last_speak_time` cũ bị tách làm hai** — `last_external_activity` (người khác nói) và `last_self_speak` (Mai tự nói), vì hai cái này có ý nghĩa hoàn toàn khác nhau và không nên dùng chung 1 biến.

### 2.2. `CategorySelector` — thay prompt chung chung 7.9.4

Thay vì 1 khối "có thể càm ràm/kể chuyện/hỏi/gọi ông" nhét chung vào 1 prompt, tách thành category rõ ràng, mỗi category có cooldown riêng + trọng số riêng theo mood:

```yaml
# config/autonomy.yaml
categories:
  complain_silence:      # càm ràm vì im lặng
    weight: 1.0
    cooldown_seconds: 300
    mood_boost: { bon_chon: 1.5 }
    prompt_hint: "Càm ràm nhẹ về việc chat im re, kiểu trách móc đùa"

  share_thought:         # kể chuyện vặt / suy nghĩ ngẫu nhiên
    weight: 1.2
    cooldown_seconds: 240
    mood_boost: { vui: 1.2 }
    prompt_hint: "Kể 1 chuyện vặt/suy nghĩ bất chợt, không liên quan chat"

  ask_chat:              # chủ động hỏi chat
    weight: 1.0
    cooldown_seconds: 200
    mood_boost: {}
    prompt_hint: "Đặt 1 câu hỏi ngắn cho chat, kiểu tò mò"

  call_operator:         # gọi ông
    weight: 0.6
    cooldown_seconds: 400
    mood_boost: { bon_chon: 1.3, buon: 1.1 }
    prompt_hint: "Gọi ông kiểu ngang/trêu, không khẩn cầu thật (persona.md Phần C)"

  follow_up_topic:       # nhắc lại chủ đề gần đây (cần Memory Phase 7, tạm dùng working memory)
    weight: 0.8
    cooldown_seconds: 350
    mood_boost: {}
    prompt_hint: "Quay lại 1 chủ đề vừa nhắc trước đó trong session, thêm ý mới"

no_repeat_window: 2        # không chọn lại category vừa dùng trong 2 lần gần nhất
```

```python
class CategorySelector:
    def __init__(self, cfg: AutonomyConfig):
        self.cfg = cfg
        self.last_used: dict[str, datetime] = {}

    def select(self, mood: MoodState, recent: deque[str]) -> str:
        candidates = []
        for name, c in self.cfg.categories.items():
            if name in recent:                                  # vừa dùng gần đây → loại
                continue
            if self._on_cooldown(name, c.cooldown_seconds):      # riêng category này còn cooldown
                continue
            w = c.weight
            for dim, mult in c.mood_boost.items():
                w *= 1 + (getattr(mood, dim) / 10) * (mult - 1)
            candidates.append((name, w))

        if not candidates:              # tất cả đang cooldown → fallback category rẻ nhất
            return "share_thought"

        return weighted_random_choice(candidates)
```

### 2.3. Prompt content — thay 7.9.4

Prompt cụ thể hoá theo category đã chọn, thay vì để LLM tự đoán trong 1 danh sách chung:

```
[System: Persona]
[Context:
  - Im lặng đã: {silence_duration}
  - Mood hiện tại: {current_mood}
  - Lý do Mai muốn nói lúc này: {category.prompt_hint}
  - (nếu follow_up_topic) Chủ đề gần đây: {recent_memory_snippet}
]

Mai tự lên tiếng theo đúng lý do trên. Không nói câu quá dài. Đúng chất Mai.
Không lặp lại nguyên văn cách nói lần tự nói trước.
```

`prompt_hint` cụ thể giúp model không phải tự chọn ngẫu nhiên trong đầu (nơi nó dễ lệch về 1-2 pattern quen) — việc "chọn kiểu gì" đã được `CategorySelector` quyết ở tầng code, model chỉ cần thực thi đúng kiểu đó bằng giọng Mai.

---

## 2.4. Bóc tách chi tiết: Pipeline sinh nội dung (bên trong 1 category)

Mục 2.3 mới dừng ở "category khác nhau → prompt_hint khác nhau". Nhưng nếu bên trong 1 category vẫn để LLM tự bịa hoàn toàn mỗi lần, nó vẫn lặp — không phải lặp category, mà lặp **cách mở câu, cấu trúc câu, chủ đề cụ thể**. Đây là lỗi LLM rất hay gặp: dù prompt hint khác, model có xu hướng quay về 2-3 pattern quen (kiểu luôn mở đầu bằng "Ơ", luôn kết bằng câu hỏi tu từ...).

Bóc thành 5 bước, tách rõ **nguồn nguyên liệu** khỏi **hành động generate**:

```
Bước 1: MATERIAL RETRIEVAL      → lấy dữ kiện thật, không để LLM tự bịa số/sự kiện
Bước 2: SLOT FILL                → nhét dữ kiện vào template cụ thể theo category
Bước 3: ANTI-REPEAT CONSTRAINT   → liệt kê cái KHÔNG được lặp (opener, chủ đề vừa dùng)
Bước 4: GENERATE                 → LLM call, prompt đã cụ thể hoá từ bước 1-3
Bước 5: POST-CHECK DEDUP         → so với N lần tự nói gần nhất, quá giống thì regenerate 1 lần
```

### Bước 1 — Material Retrieval: nguồn nguyên liệu theo từng category

Vấn đề của bản gốc: `prompt_hint` là 1 câu mô tả trừu tượng ("kể chuyện vặt") — LLM phải tự nghĩ ra chuyện gì, dễ bịa lặp lại ý cũ hoặc sai lore. Sửa: mỗi category có **nguồn dữ kiện cụ thể**, không phải mô tả chung:

| Category | Nguyên liệu lấy từ đâu | Vì sao không để LLM tự bịa |
|---|---|---|
| `complain_silence` | Số liệu thật: `silence_duration`, số tin chat trong 10 phút gần nhất, giờ hiện tại | Con số cụ thể → càm ràm có "cớ" thật, không chung chung |
| `share_thought` | 1 item lấy từ **topic pool** (config, xoay vòng không lặp — xem dưới) | Nếu để LLM tự nghĩ "chuyện vặt" mỗi lần → hội tụ về vài chủ đề quen thuộc của model, không đa dạng thật |
| `ask_chat` | 1 **question template** lấy từ pool theo loại (ý kiến / sự kiện gần đây / cá nhân), xoay vòng | Tránh việc luôn hỏi kiểu câu giống nhau ("mọi người thấy sao") |
| `call_operator` | Trạng thái operator (online/offline nếu detect được) + `consecutive_ignored` count | Gọi ông có lý do bám vào tình huống thật, không phải ngẫu nhiên |
| `follow_up_topic` | 1-2 entry mới nhất từ working memory (Phase 1 đã có deque, chưa cần chờ Phase 7 semantic) | Bắt buộc phải có nguồn thật — nếu không, category này nên bị loại khỏi candidate list (xem `select()` ở 2.2, thêm điều kiện: không có memory thì skip category) |

```python
class MaterialProvider:
    def get(self, category: str, ctx: RuntimeContext) -> dict:
        match category:
            case "complain_silence":
                return {
                    "silence_seconds": ctx.silence_seconds,
                    "chat_count_10min": ctx.chat_activity.count_last(minutes=10),
                }
            case "share_thought":
                topic = self.topic_pool.next("share_thought")  # xoay vòng, xem dưới
                if topic is None:
                    return None   # hết pool / đang cooldown hết → category này không khả dụng lượt này
                return {"topic_seed": topic}
            case "ask_chat":
                q = self.question_pool.next()
                return {"question_seed": q}
            case "call_operator":
                return {
                    "operator_online": ctx.operator_presence,
                    "ignored_streak": ctx.consecutive_ignored,
                }
            case "follow_up_topic":
                recent = ctx.working_memory.last(n=2)
                if not recent:
                    return None   # không có gì để follow-up → category không khả dụng
                return {"memory_snippet": recent}
```

**Điểm quan trọng:** nếu `get()` trả `None` (không có nguyên liệu thật), category đó bị loại khỏi candidate ở `CategorySelector.select()` (mục 2.2) — **không bao giờ để LLM tự bịa khi không có material**. Đây là khác biệt lớn nhất so với bản gốc, nơi model luôn phải tự nghĩ ra nội dung từ con số 0.

### Bước 1b — Topic pool / Question pool: kho nguyên liệu tĩnh, tiêu thụ dần

```yaml
# config/autonomy_content_pool.yaml
share_thought_pool:
  - "chuyện vừa nghĩ ra về việc chơi game với ông"
  - "thắc mắc vu vơ vì sao mọi người hay thức khuya xem stream"
  - "than thở về việc chưa có tay chân thật để làm gì đó"
  # ... 15-30 seed, KHÔNG phải câu thoại có sẵn — chỉ là "hạt giống ý tưởng"
  # LLM vẫn viết câu thật theo giọng Mai, seed chỉ định hướng chủ đề

question_pool:
  opinion: ["mọi người thích chơi game gì nhất", "ai từng bị mất ngủ chưa"]
  personal: ["hôm nay ai đó có chuyện gì vui không", "có ai đang ăn gì không"]

pool_policy:
  no_repeat_last_n: 8        # 1 seed dùng rồi thì 8 lần tự nói tiếp theo không dùng lại
  reshuffle_when_exhausted: true
```

Cách này khác cách cũ ở chỗ: bản gốc để LLM **tự nghĩ chủ đề từ đầu** mỗi lần (dễ hội tụ pattern quen) — bản mới cho LLM **1 hạt giống chủ đề cụ thể, khác nhau mỗi lần theo vòng xoay**, LLM chỉ lo phần diễn đạt bằng giọng Mai. Việc "đa dạng chủ đề" chuyển từ trách nhiệm của LLM (không kiểm soát được) sang trách nhiệm của config (kiểm soát được, N6).

### Bước 2 — Slot fill: template cụ thể thay vì mô tả chung

```
[System: Persona]
[Context:
  - Lý do: complain_silence
  - Đã im lặng: {silence_seconds}s, chat có {chat_count_10min} tin trong 10 phút qua
  - Mood: {current_mood}
]
[KHÔNG được mở đầu bằng: {forbidden_openers}]     ← xem bước 3

Càm ràm nhẹ, đúng chất Mai, dựa đúng số liệu trên (đừng bịa số khác).
Câu ngắn, 1-2 câu.
```

### Bước 3 — Anti-repeat constraint: chặn ở tầng câu chữ, không chỉ tầng category

Track N câu tự nói gần nhất → trích ra **3 từ mở đầu** của mỗi câu → đưa vào prompt như constraint tường minh:

```python
class OpenerTracker:
    def __init__(self, window: int = 5):
        self.recent_openers: deque[str] = deque(maxlen=window)

    def forbidden_list(self) -> str:
        return ", ".join(f'"{o}..."' for o in self.recent_openers) or "(không có)"

    def record(self, text: str) -> None:
        opener = " ".join(text.split()[:3])
        self.recent_openers.append(opener)
```

Đây là chỗ rẻ nhất nhưng hiệu quả nhất — model bị chặn tường minh không được lặp câu mở đầu, thay vì hy vọng nó "tự nhiên đa dạng".

### Bước 4 — Generate: không đổi, gọi LLM service hiện có (8.2)

### Bước 5 — Post-check dedup: bắt lặp còn sót sau generate

Không cần embedding/semantic phức tạp — 1 check rẻ bằng token overlap là đủ cho MVP (N1: đừng build phức tạp khi chưa cần):

```python
def is_too_similar(new_text: str, recent_texts: list[str], threshold: float = 0.6) -> bool:
    new_tokens = set(new_text.lower().split())
    for old in recent_texts:
        old_tokens = set(old.lower().split())
        overlap = len(new_tokens & old_tokens) / max(1, len(new_tokens | old_tokens))
        if overlap > threshold:
            return True
    return False

# Trong orchestrator sau khi generate xong:
if is_too_similar(generated_text, recent_ambient_outputs[-5:]):
    generated_text = await regenerate_once(prompt)   # thử lại đúng 1 lần, không loop vô hạn
```

Nếu regenerate lần 2 vẫn giống → chấp nhận phát (fail-open theo N7, đừng chặn hẳn tự nói vì lỗi dedup).

### Tóm gọn khác biệt

| | Bản gốc (7.9.4) | Bản mới |
|---|---|---|
| Nguồn nội dung | LLM tự bịa từ 1 câu mô tả chung | Material cụ thể (số liệu thật / seed từ pool) bơm vào slot |
| Đa dạng chủ đề | Hy vọng LLM tự đa dạng | Pool xoay vòng, config kiểm soát (N6) |
| Lặp cách mở câu | Không kiểm soát | `OpenerTracker` chặn tường minh trong prompt |
| Lặp toàn câu | Không kiểm soát | Post-check token-overlap, regenerate 1 lần |
| Category không có data thật | Vẫn generate (bịa) | Bị loại khỏi candidate (`MaterialProvider` trả `None`) |

---

## 3. Tích hợp vào TriggerManager hiện có

Chỉ thay 2 method, giữ nguyên toàn bộ phần còn lại của 7.9.2:

```python
class TriggerManager:
    def __init__(self):
        self.queue: PriorityQueue = PriorityQueue()
        self.chat_rate_limiter = SimpleRateLimiter(window_seconds=10, max_events=3)
        self.autonomy = AutonomyEngine(load_config("autonomy.yaml"))  # MỚI

    async def get_next_trigger(self) -> Trigger | None:
        self._prune_expired()
        if not self.queue.empty():
            return self.queue.get()

        if self.autonomy.should_speak_now():
            category = self.autonomy.select_category()
            return self._create_ambient_trigger(category)
        return None

    def _create_ambient_trigger(self, category: str) -> Trigger:
        return Trigger(
            type=TriggerType.AMBIENT_TALK,
            event=InputEvent(
                source=EventSource.SYSTEM_TIMER,
                content="",
                metadata={"mode": "ambient", "category": category},
            ),
            priority=TriggerType.AMBIENT_TALK.priority,
        )

    async def process_event(self, event: InputEvent) -> TriggerDecision:
        # ... logic cũ giữ nguyên ...
        self.autonomy.on_external_activity()   # MỚI — reset đúng nghĩa
        ...
```

Sau khi Mai nói xong (TTS/orchestrator biết turn đã kết thúc), gọi:

```python
if trigger.type == TriggerType.AMBIENT_TALK:
    trigger_manager.autonomy.on_self_spoke()
```

**Không cần đổi:** state machine, interrupt policy 7.9.3, priority value (`ambient_talk: 10`), số trigger type (vẫn 4 — N1 giữ nguyên).

---

## 4. Test / Definition of Done cho module này

Thêm vào Section 12 (Testing) và DoD Phase tương ứng:

| Test | Tiêu chí pass |
|---|---|
| **Variance test** | Chạy giả lập 4h (mock clock), khoảng cách giữa các lần tự nói KHÔNG phải hằng số — độ lệch chuẩn > 0 và không có 2 lần cách nhau lệch <5% (tránh y hệt lặp lại) |
| **No-repeat category** | Trong 20 lần tự nói liên tiếp (giả lập), không category nào xuất hiện 2 lần liên tiếp; không category nào chiếm >40% tổng |
| **Self-cooldown** | Ngay sau `on_self_spoke()`, `should_speak_now()` phải False trong ít nhất `self_cooldown_seconds` |
| **Mood coupling** | Set `bon_chon=9` so với `bon_chon=1` cùng điều kiện khác — thời gian trung bình tới lần tự nói tiếp theo phải ngắn hơn rõ rệt (đo qua N lần giả lập) |
| **Nag decay** | Giả lập 5 lần tự nói liên tiếp không có `on_external_activity()` xen giữa — xác suất tự nói lần 5 phải thấp hơn lần 1 |
| **Subjective (live)** | Chạy live ≥2h, user chấm điểm "có thấy máy móc/lặp lại không" — target: không phát hiện pattern rõ ràng qua quan sát thường |

Gợi ý dùng chính VOD analysis Neuro-sama bạn đang làm (observation template) để lấy con số thật cho `rise_curve`, `probability_curve`, và phân phối category weight — thay vì đoán số, giống tinh thần Pre-flight spike (đo trước, đừng giả định).

---

## 5. Vị trí trong roadmap

Theo PROCESS.md hiện tại, `ambient_talk` nằm trong Phase 2 (Trigger + State Machine), còn Mood đầy đủ (Appraisal Engine) ở Phase 7.5 — cách nhau rất xa. Đề xuất:

- **Không chờ Phase 7.5.** Dùng mood v1 self-report (đã có sẵn từ Phase 1, format persona.md Phần B) làm input cho `UrgeAccumulator` ngay từ Phase 2. Khi Phase 7.5 xong, chỉ đổi nguồn cung cấp `MoodState` (từ parser LLM → từ MoodEngine spring-damper), kiến trúc autonomy không đổi gì.
- Việc này khớp với input trước đó của bạn rằng autonomy tick đang được đánh giá lại là **cấp bách hơn** dự kiến ban đầu — module này không phụ thuộc TTS/Voice hoàn chỉnh, test được ngay qua CLI text (Phase 1 đã có), nên có thể làm sớm mà không phá thứ tự phase khác.
- Cập nhật `Appendix C` (trade-off log): thêm dòng "Autonomy trigger: probabilistic + category-based (v2) — thay hard threshold 60s (v2.1 gốc) — lý do: quan sát thật thấy step-function tạo pattern máy móc, đo được qua VOD analysis Neuro-sama".

---

## 6. Việc cần làm (checklist implement)

- [ ] `config/autonomy.yaml` — categories, cooldown, mood_boost, `rise_curve`/`probability_curve` params
- [ ] `orchestrator/autonomy_engine.py` — `UrgeAccumulator` + `CategorySelector`
- [ ] Sửa `TriggerManager.get_next_trigger` / `process_event` (2 chỗ, giữ nguyên còn lại)
- [ ] Prompt template category-specific (thay khối chung 7.9.4)
- [ ] Dashboard: thêm mini-chart urge score realtime (tận dụng metric infra Phase 0 đã có) — giúp bạn debug "tại sao Mai chưa tự nói" trực quan thay vì đọc log
- [ ] 5 test ở mục 4
- [ ] Cập nhật `STATE.md` + `Appendix C` như mục 5
