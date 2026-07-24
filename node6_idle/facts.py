import json
import os
import sqlite3

from . import config as C

# node6 gom DỮ KIỆN THÔ, KHÔNG diễn giải (nguyên tắc 1). Nguồn:
#   - mood: đọc thẳng SQLite node2 ghi (chỉ đọc).
#   - nhịp phòng + voice: file node1 công bố (best-effort).
#   - mai_silent / auto_streak / recent_lines: node6 tự nhớ (xem node6.py).

_MOOD_DEFAULT = {"vui": 0, "buon": 0, "buc": 0, "bon_chon": 0, "nguong": 0,
                 "ly_do": "", "updated_at": 0.0}


def read_mood(db_path: str = None) -> dict:
    """Mood hiện tại (CHỈ đọc). Thiếu file / lỗi -> mood 0 (không làm chết node6)."""
    path = db_path or C.MOOD_DB_PATH
    try:
        if not os.path.exists(path):
            return dict(_MOOD_DEFAULT)
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT vui, buon, buc, bon_chon, nguong, ly_do, updated_at "
                "FROM mood WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return dict(_MOOD_DEFAULT)
        keys = ["vui", "buon", "buc", "bon_chon", "nguong", "ly_do", "updated_at"]
        return dict(zip(keys, row))
    except Exception:
        return dict(_MOOD_DEFAULT)


def read_room_state(path: str = None) -> dict:
    """Nhịp phòng + voice node1 công bố. Thiếu / lỗi -> rỗng (mặc định an toàn:
    urge không có tín hiệu -> Mai im, thà im còn hơn chen bừa)."""
    path = path or C.ROOM_STATE_PATH
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def build_facts(state, now: float, mood: dict, room: dict) -> dict:
    """Ghép dữ kiện node6-tự-nhớ (`state`) + mood + room thành 1 dict phẳng.
    `mai_silent_sec` xấp xỉ bằng thời gian từ lần mood đổi gần nhất — mỗi lượt
    Mai nói (trả lời hay tự nói) node2 đều ghi mood, nên đây là 'Mai im bao lâu'."""
    updated = mood.get("updated_at", 0.0) or 0.0
    mai_silent = max(0.0, now - updated) if updated > 0 else 0.0
    return {
        "voice_members": room.get("voice_members", []),
        "voice_count": room.get("voice_count", 0),
        "just_joined": room.get("just_joined", []),
        "just_left": room.get("just_left", []),
        "chat_silent_sec": room.get("chat_silent_sec"),
        "mai_silent_sec": mai_silent,
        "operator_silent_sec": room.get("operator_silent_sec"),
        "mai_called_unanswered": room.get("mai_called_unanswered", 0),
        "auto_streak": state.auto_streak,
        "pending_topic": state.pending_topic,
        "recent_mai_lines": list(state.recent_mai_lines),
        "room_pulse": room.get("room_pulse", {}),
        "someone_speaking": room.get("someone_speaking", False),
    }
