from dataclasses import dataclass, field, asdict
from typing import Optional

# Mỗi giai đoạn CHỈ ghi thêm mốc của mình. Không đọc/sửa mốc của giai đoạn khác.
@dataclass
class TimingTrace:
    turn_id: str
    t0_received: Optional[float] = None        # Node1
    t1_enqueued: Optional[float] = None        # Node1->Node2
    t2_llm_start: Optional[float] = None       # Node2
    t3_llm_end: Optional[float] = None         # Node2
    t4_moderation_end: Optional[float] = None  # Node2
    t6_persona_end: Optional[float] = None     # Node2.5
    t7_handoff_node3: Optional[float] = None   # Node2/2.5->Node3
    t8_node3_received: Optional[float] = None  # Node3
    t9_audio_start: Optional[float] = None     # Node3

    def to_dict(self) -> dict: return asdict(self)

    def deltas(self) -> dict:
        # Chỉ tính delta khi cả 2 mốc tồn tại (bỏ mốc thiếu, không chặn).
        def d(a, b): return None if a is None or b is None else round(b - a, 2)
        t = self
        return {
            "node1_proc":      d(t.t0_received, t.t1_enqueued),
            "queue_wait":      d(t.t1_enqueued, t.t2_llm_start),
            "llm_call":        d(t.t2_llm_start, t.t3_llm_end),
            "moderation":      d(t.t3_llm_end, t.t4_moderation_end),
            "cut_handoff":     d(t.t6_persona_end, t.t7_handoff_node3),
            "node3_to_audio":  d(t.t8_node3_received, t.t9_audio_start),
            "end_to_end":      d(t.t0_received, t.t9_audio_start),
        }