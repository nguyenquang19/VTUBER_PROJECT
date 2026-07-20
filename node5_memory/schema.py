from dataclasses import dataclass, field, asdict
from enum import Enum
import time

class ModStatus(str, Enum):
    APPROVED = "approved"; PENDING = "pending"; REJECTED = "rejected"

@dataclass
class UserProfile:
    user_id: str
    display_name: str
    first_seen: float = field(default_factory=time.time)
    appearances: int = 0
    summary: str = ""                     # 1-2 câu, model viết lại sau mỗi buổi
    last_mentioned: float = field(default_factory=time.time)
    memory_strength: float = 1.0          # giảm dần theo thời gian (quên dần)
    mod_status: ModStatus = ModStatus.PENDING
    def to_dict(self):
        d = asdict(self); d["mod_status"] = self.mod_status.value; return d

@dataclass
class ModerationLog:
    user_id: str
    session_id: str                       # chống ghi trùng
    ts: float
    old_summary: str
    new_summary: str
    decision: str
    source: str                           # "auto_endsession" | "manual"