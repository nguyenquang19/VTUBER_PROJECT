import json
import socket
import time
import uuid

from shared.contracts.payloads import IngestionRecord, EventType
from shared.contracts.timing import TimingTrace
from shared.utils.logger import get_logger
from . import config as C
from .facts import read_mood, read_room_state, build_facts
from .urge import should_speak

_log = get_logger("node6")


class Node6State:
    """Những gì CHỈ node6 biết (DAC-TA 4.3). node6 không gọi LLM, không tới
    node3 — nó chỉ đếm rồi đẩy việc vào node2."""

    def __init__(self):
        self.last_auto_ts = 0.0     # lần cuối node6 đẩy lượt tự nói
        self.auto_streak = 0        # số lượt tự nói LIÊN TIẾP (đứt khi có tương tác)
        self.recent_mai_lines = []  # câu tự nói gần đây (điền khi có feedback node2)
        self.pending_topic = ""     # chuyện đang dở (từ tín hiệu "còn nữa")
        self.braked_logged = False  # đã log chạm phanh chưa (tránh spam mỗi tick)

    def note_auto_sent(self, ts: float):
        self.last_auto_ts = ts
        self.auto_streak += 1

    def reset_streak(self):
        # Có tương tác thật (chat mới) -> chuỗi tự nói đứt, phanh nhả.
        self.auto_streak = 0
        self.braked_logged = False


def send_auto_request(facts: dict, host: str = None, port: int = None) -> None:
    """Đẩy 1 lượt AUTO vào ingress node2 — cùng đường node1 dùng, cùng cửa
    moderation. node6 KHÔNG có đường nào đi thẳng node3 (chống đường tắt)."""
    trace = TimingTrace(turn_id=str(uuid.uuid4()))
    rec = IngestionRecord(EventType.AUTO, "__auto__", "Mai", "",
                          time.time() * 1000, 0.0, trace, facts)
    line = (json.dumps(rec.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
    with socket.create_connection((host or C.NODE2_HOST, port or C.NODE2_PORT),
                                  timeout=2) as s:
        s.sendall(line)


def tick(state: Node6State, now: float) -> bool:
    """Một nhịp: gom dữ kiện -> quyết định. Trả True nếu đã đẩy lượt tự nói.
    Tách khỏi vòng lặp để test được không cần thời gian thật."""
    mood = read_mood()
    room = read_room_state()
    facts = build_facts(state, now, mood, room)

    # Chuỗi tự nói đứt khi có chat mới (chat vừa im rất ngắn = ai đó vừa nhắn).
    cs = facts.get("chat_silent_sec")
    if cs is not None and cs < C.TICK_SEC * 2:
        state.reset_streak()

    if not should_speak(facts, mood):
        return False

    if state.auto_streak >= C.MAX_AUTO_STREAK:
        # Phanh an toàn (§5): dừng tự nói tới khi có tương tác. Log 1 lần.
        if not state.braked_logged:
            _log.info(f"phanh an toàn: chạm MAX_AUTO_STREAK={C.MAX_AUTO_STREAK}, "
                      "dừng tự nói tới khi có tương tác")
            state.braked_logged = True
        return False

    try:
        send_auto_request(facts)
    except Exception as e:
        _log.info(f"gửi AUTO lỗi (bỏ qua tick này): {e}")
        return False
    state.note_auto_sent(now)
    _log.info(f"AUTO đẩy vào node2 (streak={state.auto_streak})")
    return True


def run_forever(tick_sec: float = None):
    interval = tick_sec or C.TICK_SEC
    state = Node6State()
    _log.info(f"node6 tự nói: tick={interval}s, max_streak={C.MAX_AUTO_STREAK}")
    while True:
        try:
            tick(state, time.time())
        except Exception as e:
            _log.info(f"node6 tick lỗi (bỏ qua): {e}")
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
