from dataclasses import dataclass, asdict
from enum import Enum, IntEnum
from .timing import TimingTrace

class EventType(str, Enum):
    CHAT = "chat"
    AUTO = "auto"           # lượt Mai tự nói (node6 đẩy vào)

# Hợp đồng Node1 -> Node2 (BẤT BIẾN với lượt CHAT).
# `facts` là field optional (default None) -> lượt CHAT cũ không đổi. Chỉ lượt
# tự nói (node6) mới mang facts; khi có facts thì đây là lượt AUTO.
@dataclass
class IngestionRecord:
    event_type: EventType
    user_id: str
    display_name: str
    content: str
    sent_at: float          # epoch ms, thời điểm Discord gửi
    tier1_score: float      # rule-based
    timing: TimingTrace
    facts: dict | None = None    # dữ kiện cho lượt tự nói; None = lượt thường

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        return d

class Mood(str, Enum):
    NEUTRAL = "neutral"; HAPPY = "happy"; SAD = "sad"; EXCITED = "excited"

class Priority(IntEnum):
    AUTO = 0; CHAT = 1; CRISIS = 2   # số lớn = ưu tiên cao

# Hợp đồng Node2 -> Node3 (BẤT BIẾN): luồng câu đã cắt sẵn.
@dataclass
class SentenceOut:
    turn_id: str
    seq: int              # thứ tự câu trong turn
    text: str
    is_last: bool
    mood: Mood
    def to_dict(self): d = asdict(self); d["mood"] = self.mood.value; return d

# Kênh riêng "dừng ngay" cho 1 turn cụ thể. Node3 chỉ cần turn_id, không cần lý do.
@dataclass
class StopSignal:
    turn_id: str
    def to_dict(self): return asdict(self)