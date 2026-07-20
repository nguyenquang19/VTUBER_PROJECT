from dataclasses import dataclass, asdict, field
from enum import Enum

class EventKind(str, Enum):
    STATE = "state"        # Node2 đổi trạng thái
    QUEUE = "queue"        # kích thước hàng đợi thay đổi
    CHAT = "chat"          # tin nhắn vào
    REPLY = "reply"        # AI trả lời (text)
    CRISIS = "crisis"      # cảnh báo crisis
    TIMING = "timing"      # breakdown độ trễ 1 turn
    AUDIO = "audio"        # Node3 phát câu

# Sự kiện 1 CHIỀU Node1/2/3 -> Node4. Fire-and-forget, Node4 chết không sao.
@dataclass
class DashboardEvent:
    kind: EventKind
    ts: float
    data: dict = field(default_factory=dict)
    def to_dict(self):
        d = asdict(self); d["kind"] = self.kind.value; return d

# Lệnh điều khiển Dashboard -> Node2 qua CỔNG RIÊNG (không qua Node4).
# v1 chỉ có SKIP.
@dataclass
class ControlCommand:
    action: str          # "skip"
    turn_id: str = ""    # turn cần skip; rỗng = turn đang nói
    def to_dict(self): return asdict(self)