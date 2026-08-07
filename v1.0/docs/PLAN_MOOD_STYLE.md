# PLAN — Mood → Style (mood lái giọng nói LLM)

> Spec cho AI agent thực thi. Mục tiêu: mood engine không chỉ hiện trên dashboard mà
> LÁI CÁCH NÓI của LLM — bực nói cộc/gắt, vui nói tươi/lầy, buồn nói trầm/chậm…
> Nguyên tắc: config DÀY (phủ đủ trạng thái) + inject MỎNG (1 ô/lượt) + chữ tune từ data.
> Config-over-code (N6), fail-safe (N7). 1 task = 1 commit + test.

---

## 0. Bối cảnh + vì sao

Hiện `_format_mood_context` (services/llm/prompt_manager.py) chèn mood dạng SỐ THÔ mỗi lượt:
```
- current_mood: vui=3 buon=2 buc=8 bon_chon=4 nguong=1
- event_category: chat_compliment
```
→ LLM dịch "buc=8" thành giọng nói không đáng tin (số quá trừu tượng), và số + nhãn
snake_case kéo register về kiểu máy móc (đây là 1 phần "câu từ chưa tốt").

**Fix:** engine mood → tra bảng `mood_style.yaml` (chiều trội + band) → bơm 1 **chỉ dẫn giọng
bằng chữ** thay cho số. Đây là bản ĐÚNG của mood block cũ: dùng mood làm INPUT điều kiện
(bảo LLM nói sao), không phải OUTPUT tự khai (gây máy móc).

---

## TASK 1 — Tạo `config/mood_style.yaml`

Copy nguyên văn dưới đây. Chữ trong ô là NHÁP giọng Mai (ngang/cà khịa) — tune sau từ transcript.

```yaml
# config/mood_style.yaml
# Mood → chỉ dẫn giọng. Engine mood (chiều trội + band) → 1 ô → bơm 2-3 dòng vào prompt.
# Config DÀY (phủ đủ), inject MỎNG (1 ô/lượt). 4 trục cố định: thai_do/nhip/do_dai/tu_dem.

policy:
  inject_floor: 6            # dominant < 6 → KHÔNG bơm chỉ dẫn (để persona default nói)
  bands:                     # giá trị dominant → tên band
    mid:  [6, 7]
    high: [8, 9]
    peak: [10, 10]
  secondary_enabled: false   # (mở rộng sau) true → thêm 1 mệnh đề phụ nếu chiều nhì cách trội ≤1 và ≥floor
  tone_flag_overrides: true  # có force_gentle_tone/force_deflect → BỎ mood style (flag thắng)

# Nhãn tiếng Việt cho render
labels:
  vui: "vui"
  buon: "buồn"
  buc: "bực"
  bon_chon: "bồn chồn"
  nguong: "ngượng"
band_prefix:                 # mức độ chèn trước tên mood khi render
  mid: ""
  high: "khá "
  peak: "cực kỳ "

styles:
  buc:
    mid:  { thai_do: "hơi xẵng, bớt đùa, dễ cáu vặt", nhip: "đáp nhanh hơn thường", do_dai: "câu ngắn lại", tu_dem: "hứ, gì đấy, ừ thì" }
    high: { thai_do: "cộc, gắt, mỉa, không xuống nước", nhip: "đốp lại ngay, thiếu kiên nhẫn", do_dai: "câu cụt 1-2 câu", tu_dem: "hứ, gì, thôi đi, biết rồi" }
    peak: { thai_do: "nổ, chửi yêu, mất kiên nhẫn hẳn", nhip: "gắt gỏng, như muốn ngắt lời", do_dai: "1 câu cụt lủn", tu_dem: "trời, thôi khỏi, dẹp" }
  vui:
    mid:  { thai_do: "vui vẻ, thoải mái, đùa nhẹ", nhip: "nhẹ nhõm", do_dai: "bình thường", tu_dem: "hí, ừ nhỉ, cũng được" }
    high: { thai_do: "phấn khích, trêu tợn, đùa nhiều", nhip: "nhanh, năng lượng cao", do_dai: "kể dài hơn, hào hứng", tu_dem: "á, luôn, hihi, đỉnh, xịn" }
    peak: { thai_do: "phởn hết cỡ, lầy, cười khích", nhip: "liến thoắng", do_dai: "nói liền mạch, tràn ra", tu_dem: "trời ơi, đỉnh của chóp, hahaha" }
  buon:
    mid:  { thai_do: "hơi tụt mood, bớt lầy", nhip: "chậm lại chút", do_dai: "ngắn, ít hào hứng", tu_dem: "ừ, thôi, chả biết" }
    high: { thai_do: "buồn thật, trầm, ít đùa", nhip: "chậm, ngập ngừng, hay thở dài", do_dai: "cụt, lười nói", tu_dem: "hầy, thôi kệ, chán" }
    peak: { thai_do: "xị hẳn, gần như không muốn nói", nhip: "rời rạc, ngắt quãng", do_dai: "1 câu ngắn hoặc lảng", tu_dem: "..., thôi bỏ đi" }
  bon_chon:
    mid:  { thai_do: "hơi sốt ruột, nhấp nhổm", nhip: "nhanh hơn thường", do_dai: "câu ngắn, hay hỏi", tu_dem: "ơ, sao thế, nhanh lên" }
    high: { thai_do: "bồn chồn rõ, đứng ngồi không yên", nhip: "hớt hải, hỏi dồn", do_dai: "hỏi liên tiếp, cụt", tu_dem: "ơ ơ, gì vậy, đâu rồi" }
    peak: { thai_do: "cuống, lo ra mặt", nhip: "dồn dập, nói vấp", do_dai: "hỏi dồn không chờ đáp", tu_dem: "trời ơi sao thế, đâu rồi đâu" }
  nguong:
    mid:  { thai_do: "hơi ngại, lảng nhẹ", nhip: "chững lại chút", do_dai: "ngắn, né chủ đề", tu_dem: "gì mà, đâu có, thôi" }
    high: { thai_do: "ngượng rõ, chối đây đẩy", nhip: "ngập ngừng, ấp úng", do_dai: "cụt, đổi chủ đề", tu_dem: "gì mà kỳ vậy, đâu có" }
    peak: { thai_do: "quê độ, giãy nảy chối", nhip: "lắp bắp, gắt để giấu ngượng", do_dai: "cụt, chuyển chủ đề gấp", tu_dem: "ơ hay, ai bảo, thôi đủ rồi" }
```

**Test:** load yaml → có đủ 5 chiều, mỗi chiều 3 band, mỗi ô đủ 4 trục.

---

## TASK 2 — Đăng ký config vào loader

`orchestrator/config_loader.py`: thêm `"mood_style": "mood_style.yaml"` vào bảng CONFIG_FILES
(như `autonomy` đã thêm). Đảm bảo nằm trong danh sách hot-reload watchdog → tune chữ không cần restart.

**Test:** `loader.get("mood_style", "styles.buc.high.thai_do")` trả đúng chuỗi.

---

## TASK 3 — Logic tra bảng + render (module mới)

Tạo `services/emotion/mood_style.py`:

```python
class MoodStyleTable:
    def __init__(self, policy, labels, band_prefix, styles): ...

    @classmethod
    def from_loader(cls, loader) -> "MoodStyleTable | None":
        # đọc config/mood_style.yaml; None nếu thiếu (fail-safe → không bơm gì)

    def directive_for(self, mood, tone_flags: set[str] | None) -> str | None:
        """Trả 1 chuỗi chỉ dẫn 2-3 dòng, hoặc None nếu không bơm.

        Bước:
        1. Nếu tone_flags có force_gentle_tone/force_deflect và tone_flag_overrides
           → return None (flag thắng, xử ở tầng khác).
        2. dominant = chiều có giá trị cao nhất (tie → theo thứ tự vui,buon,buc,bon_chon,nguong).
        3. val = getattr(mood, dominant). Nếu val < inject_floor → return None (vùng chết).
        4. band = map val qua policy.bands (mid/high/peak).
        5. cell = styles[dominant][band]. Render:
             "- Đang {band_prefix}{label}: {thai_do}. {nhip}. Câu {do_dai}. Hay dùng: {tu_dem}."
        6. (nếu secondary_enabled) chiều nhì cách trội ≤1 và ≥floor → thêm 1 mệnh đề ngắn.
        """
```

Render ví dụ (buc=9): `- Đang khá bực: cộc, gắt, mỉa, không xuống nước. Đốp lại ngay, thiếu kiên nhẫn. Câu cụt 1-2 câu. Hay dùng: hứ, gì, thôi đi.`

**Fail-safe (N7):** mọi lỗi tra bảng → return None (không bơm), KHÔNG raise.

**Test `tests/unit/test_mood_style.py`:**
- buc=9 → directive chứa "cộc"/"gắt", KHÔNG chứa "buc=" hay số.
- mood toàn ≤5 (gần baseline) → return None (vùng chết).
- tie vui=8,buc=8 → chọn vui (thứ tự khai báo).
- force_gentle_tone active → return None.
- band biên: val=7→mid, 8→high, 10→peak.

---

## TASK 4 — Nối vào `_format_mood_context` (bỏ số thô)

`services/llm/prompt_manager.py`:
- `PromptManager` nhận thêm `mood_style: MoodStyleTable | None` (from_loader wire).
- `build_request_with_mood` truyền `mood_style` xuống `_format_mood_context`.
- Trong `_format_mood_context`:
  1. **BỎ dòng** `- current_mood: vui=.. buc=..` (số thô — không đưa cho LLM nữa).
  2. **BỎ dòng** `- event_category: ...` (tag nội bộ, gây nhiễu register).
  3. GIỮ dòng A4 cause (`đang thiên về 'buc' VÌ {ai}{gì}`) — tự nhiên, bổ trợ.
  4. THÊM: `directive = mood_style.directive_for(current_mood, tone_flags)`; nếu không None → append.
  5. GIỮ tone flag hints (force_gentle_tone/force_deflect) như cũ.

Block mới ví dụ (bực vì bị troll):
```
[Context — cách nói lượt này; chỉ viết thoại]
- đang thiên về 'bực' VÌ mấy người cứ hỏi đểu — viết khớp lý do này
- Đang khá bực: cộc, gắt, mỉa, không xuống nước. Đốp lại ngay. Câu cụt 1-2 câu. Hay dùng: hứ, gì, thôi đi.
```

**Quan trọng — thứ tự ưu tiên tone vs mood:** nếu `force_gentle_tone` (người xem buồn thật) →
`directive_for` trả None + hint gentle thắng. Mood style KHÔNG được ghi đè case tổn thương thật
(persona Phần C ranh giới #4).

**Test:** build_request_with_mood với mood buc=9 → messages[1].content chứa directive chữ,
KHÔNG chứa "vui=" / "event_category". Với force_gentle_tone → không có directive bực, có hint gentle.

---

## TASK 5 — Áp cho self-talk (autonomy) luôn cho nhất quán

`services/autonomy/prompt_builder.py`: `_mood_str` đang nhét `vui=6 buc=4` vào prompt self-talk.
Thay bằng cùng `MoodStyleTable.directive_for` (hoặc bỏ hẳn dòng mood số nếu ngại truyền table).
→ Mai tự nói cũng đúng giọng theo mood, không rò số.

**Test:** render_prompt không chứa "vui=" số thô.

---

## Thứ tự + DoD

```
T1 config yaml      → T2 loader đăng ký → T3 module tra bảng (+test)
→ T4 nối prompt chat (bỏ số + event_category) → T5 self-talk
```

**DoD toàn task:**
- Prompt chat + self-talk KHÔNG còn chuỗi `vui=N buc=N` hay `event_category:`.
- mood lệch mạnh (dom ≥6) → có 1 chỉ dẫn giọng bằng chữ; gần baseline → không có (persona default).
- force_gentle_tone/force_deflect thắng mood style.
- Test unit xanh; chạy 20 turn thật, đọc transcript: giọng đổi rõ giữa bực/vui/buồn.

## Sau khi build — vòng tune (không phải 1 lần xong)
```
chạy 20-30' → lọc turns.jsonl theo mood_dominant → nghe câu Mai lúc đó
→ giọng khớp ô chưa? → sửa CHỮ trong mood_style.yaml (hot-reload, không restart)
→ lặp. Chỉnh ngưỡng band nếu mood hay rơi sai band.
```

## Liên quan (KHÔNG bắt buộc trong plan này — tham chiếu audit prompt)
- **Sampling**: request LLM hiện chỉ gửi `temperature`; nên thêm `min_p`, `repeat_penalty (~1.08)`,
  `presence_penalty` vào payload `services/llm/llama_cpp_llm.py` — đòn mạnh nhất cho register,
  nhưng làm task riêng để cô lập tác động.
- **Director read prompt**: cluster/summary/vibe là "chỉ thị sân khấu" — cân nhắc đưa vào system,
  để user turn là text chat thật (giảm giọng meta). Xem FIX_PLAN Task 5/6.
