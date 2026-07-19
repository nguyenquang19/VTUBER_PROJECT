from dataclasses import dataclass, asdict
from enum import Enum
from .timing import TimingTrace

class EventType(str, Enum):
    CHAT = "chat"

# Hợp đồng Node1 -> Node2 (BẤT BIẾN).
@dataclass
class IngestionRecord:
    event_type: EventType
    user_id: str
    display_name: str
    content: str
    sent_at: float          # epoch ms, thời điểm Discord gửi
    tier1_score: float      # rule-based
    timing: TimingTrace

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        return d