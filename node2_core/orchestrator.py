import threading
from shared.contracts.payloads import (IngestionRecord, SentenceOut,
                                        Priority, Mood, EventType)
from shared.contracts.timing import TimingTrace
from shared.utils.timing_recorder import stamp
from shared.utils.trace_sink import emit_partial
from shared.utils.logger import get_logger
from .states import State
from .queue import PriorityInbox
from .llm.llamacpp import LlamaCppClient
from .llm.fallback import fallback_sentence
from .moderation import moderate
from .crisis_stub import NoopCrisisDetector
from .splitter import split_sentences
from .history import RawHistory
from .egress import Node3Egress

_log = get_logger("node2")


class Orchestrator:
    def __init__(self, egress=None, llm=None, crisis=None):
        self.state = State.IDLE
        self.inbox = PriorityInbox()
        self.llm = llm or LlamaCppClient()
        self.crisis = crisis or NoopCrisisDetector()
        self.history = RawHistory()
        self.egress = egress or Node3Egress()
        self._speaking_turn = None      # turn_id đang phát (để ngắt khi crisis)

    # ---- ingress: Node1 (hoặc test) đẩy record vào đây ----
    def submit(self, rec: IngestionRecord):
        pri = Priority.CRISIS if self.crisis.is_crisis(rec.content) else Priority.CHAT
        # NGẮT NGAY: đang phát turn khác mà crisis tới -> stop turn đó tức thì,
        # không đợi câu hiện tại phát xong.
        if pri == Priority.CRISIS and self._speaking_turn:
            self.egress.send_stop(self._speaking_turn)
        self.inbox.put(pri, rec)

    # ---- vòng xử lý ----
    def run_once(self):
        got = self.inbox.get()
        if got is None:
            return
        pri, rec = got
        self._handle(pri, rec)

    def run_forever(self, idle_sleep=0.05):
        import time
        while True:
            if self.inbox.get_size() == 0:
                time.sleep(idle_sleep)     # NGHỈ khi rỗng -> không đốt CPU
                continue
            self.run_once()

    def _cancel_check(self):
        # Hủy generate LLM của turn thường khi có crisis đang chờ trong hàng đợi.
        return self.inbox.has_pending_crisis()

    def _handle(self, pri: Priority, rec: IngestionRecord):
        trace: TimingTrace = rec.timing
        turn_id = trace.turn_id
        session = rec.user_id
        self.state = (State.CRISIS_PRIORITY if pri == Priority.CRISIS
                      else State.PROCESSING)

        # 1) LLM (timeout cứng + retry 1 lần + fallback tĩnh)
        messages = self._build_prompt(session, rec.content)
        raw = self._call_llm(messages, trace)

        # 2) Moderation (sanitize control token + chặn cứng; hết giờ -> không phát)
        stamp(trace, "t4_moderation_end")
        ok, safe = moderate(raw)
        if not ok:
            _log.info(f"blocked turn={turn_id}")
            self.state = State.IDLE
            return

        # 3) Cắt câu -> phát sang Node3 (KHÔNG còn persona-rewrite)
        mood = Mood.NEUTRAL
        sents = split_sentences(safe)
        if not sents:
            self.state = State.IDLE
            return

        self.state = State.SPEAKING
        self._speaking_turn = turn_id
        for i, s in enumerate(sents):
            out = SentenceOut(turn_id, i, s, i == len(sents) - 1, mood)
            # timing đính vào câu đầu; egress ghi t7_handoff_node3 ở đó
            self.egress.send_sentence(out, trace if i == 0 else None)

        self.history.append(session, f"user: {rec.content}\nai: {safe}")

        # Bắn partial timing của Node2 (t2,t3,t4,t7) về collector — fire-and-forget.
        # KHÔNG ghi t9 ở đây: Node3 sở hữu t8/t9.
        emit_partial("node2", trace)

        self._speaking_turn = None
        self.state = State.IDLE

    def _build_prompt(self, session, content):
        msgs = [{"role": "system", "content":
                 "Bạn là VTuber AI tên Mai, cô gái vui vẻ thân thiện. "
                 "Trả lời bằng tiếng Việt, TỐI ĐA 2 câu ngắn, tự nhiên như đang "
                 "nói chuyện với người xem. Không dài dòng, không liệt kê, "
                 "không nhắc mình là mô hình AI hay Google."}]
        ctx = self.history.context(session)
        if ctx:
            msgs.append({"role": "system", "content": f"Ngữ cảnh trước:\n{ctx}"})
        msgs.append({"role": "user", "content": content})
        return msgs

    def _call_llm(self, messages, trace) -> str:
        for attempt in (1, 2):
            try:
                stamp(trace, "t2_llm_start")
                out = "".join(self.llm.stream(messages, self._cancel_check))
                stamp(trace, "t3_llm_end")
                if out.strip():
                    return out
            except Exception as e:
                _log.info(f"llm attempt {attempt} fail: {e}")
        stamp(trace, "t3_llm_end")
        return fallback_sentence()