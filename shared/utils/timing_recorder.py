import json
from shared.contracts.timing import TimingTrace
from shared.utils.clock import now_ms
from shared.utils.logger import get_logger

_log = get_logger("timing")

# In-process, rẻ, không I/O mạng. Lỗi ghi mốc -> bỏ qua, không raise.
def stamp(trace: TimingTrace, field: str, value: float | None = None) -> None:
    try: setattr(trace, field, value if value is not None else now_ms())
    except Exception: pass

def log_trace(trace: TimingTrace) -> None:
    # Bản tối thiểu (GĐ2+): 1 dòng JSON/turn, grep/awk được.
    _log.info(json.dumps({"turn_id": trace.turn_id,
                          "timing": trace.to_dict(),
                          "deltas": trace.deltas()}))