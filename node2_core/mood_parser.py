import re

from .mood import MOOD_DIMS, MoodState

_MOOD_LINE_RE = re.compile(r"\[[^\[\]]*vui\s*:[^\[\]]*\]", re.IGNORECASE)
# Bộ nhân vật dạy model in CÓ DẤU (buồn/bực/bồn_chồn/ngượng), còn MoodState
# dùng tên trường ASCII (buon/buc/bon_chon/nguong) -> chấp nhận cả hai kiểu.
_DIM_ALIASES = {
    "vui": "vui",
    "buồn": "buon", "buon": "buon",
    "bực": "buc", "buc": "buc",
    "bồn_chồn": "bon_chon", "bon_chon": "bon_chon",
    "ngượng": "nguong", "nguong": "nguong",
}
_DIM_RE = re.compile(
    r"(vui|bu[oồ]n|bực|buc|b[ồo]n_ch[ồo]n|ng[ưu][ợo]ng)\s*:\s*(-?\d+)",
    re.IGNORECASE,
)
_LY_DO_RE = re.compile(r"lý\s*do\s*:\s*(.+)", re.IGNORECASE)
_CON_NUA_RE = re.compile(r"còn\s*nữa\s*:\s*(có|không)", re.IGNORECASE)


def split_reply_and_mood(raw: str, prev: "MoodState") -> tuple[str, "MoodState", bool]:
    """Tách output LLM thành (câu nói, mood mới, còn_nữa).
    Model trả sai định dạng -> trả về (raw đã dọn, prev giữ nguyên, False).
    Parse MỀM: thiếu chiều nào lấy từ prev, không thấy khối mood thì bỏ qua
    toàn bộ phần chấm điểm, không làm hỏng câu nói."""
    text = raw or ""

    m = _MOOD_LINE_RE.search(text)
    if not m:
        return text.strip(), prev, False

    reply = text[:m.start()].strip()
    tail = text[m.start():]   # từ dấu [ trở đi (khối mood + lý do + còn nữa)

    dims = {}
    for name, val in _DIM_RE.findall(m.group(0)):
        key = _DIM_ALIASES.get(name.lower())
        if key:
            dims[key] = int(val)

    values = {}
    for d in MOOD_DIMS:
        values[d] = dims.get(d, getattr(prev, d))

    ly_do_match = _LY_DO_RE.search(tail)
    ly_do = ly_do_match.group(1).strip() if ly_do_match else prev.ly_do

    con_nua_match = _CON_NUA_RE.search(tail)
    con_nua = bool(con_nua_match and con_nua_match.group(1).lower() == "có")

    import time
    new_mood = MoodState(
        vui=values["vui"], buon=values["buon"], buc=values["buc"],
        bon_chon=values["bon_chon"], nguong=values["nguong"],
        ly_do=ly_do, updated_at=time.time(),
    )
    return reply, new_mood, con_nua