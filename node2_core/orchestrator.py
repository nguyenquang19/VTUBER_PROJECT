import threading, uuid
from shared.contracts.payloads import (IngestionRecord, SentenceOut,
                                       Priority, Mood, EventType)
from shared.contracts.timing import TimingTrace
from shared.utils.timing_recorder import stamp, log_trace
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
    def __init__(self, egress=None, llm=None, crisis=None, persona=None):
        self.state = State.IDLE
        self.inbox = PriorityInbox()
        self.llm = llm or LlamaCppClient()
        self.crisis = crisis or NoopCrisisDetector()
        self.history = RawHistory()
        self.egress = egress or Node3Egress()
        self._speaking_turn = None      # turn_id đang phát (để ngắt khi crisis)

    # ---- ingress: Node1 đẩy record vào đây ----
    def submit(self, rec: IngestionRecord):
        pri = Priority.CRISIS if self.crisis.is_crisis(rec.content) else Priority.CHAT
        # NGẮT NGAY: nếu đang phát 1 turn khác mà crisis tới -> stop turn đó tức thì.
        if pri == Priority.CRISIS and self._speaking_turn:
            self.egress.send_stop(self._speaking_turn)   # không đợi câu hiện tại xong
        self.inbox.put(pri, rec)

    # ---- vòng xử lý chính ----
    def run_once(self):
        got = self.inbox.get()
        if got is None: return
        pri, rec = got
        self._handle(pri, rec)

    def _cancel_check(self):
        # Hủy generate LLM của turn thường khi có crisis chờ trong hàng đợi.
        return self.inbox.has_pending_crisis()

    def _handle(self, pri: Priority, rec: IngestionRecord):
        trace: TimingTrace = rec.timing
        turn_id = trace.turn_id
        session = rec.user_id
        self.state = (State.CRISIS_PRIORITY if pri == Priority.CRISIS
                      else State.PROCESSING)

        # 1) LLM (timeout cứng + retry 1 lần + fallback tĩnh)
        prompt = self._build_prompt(session, rec.content)
        raw = self._call_llm(prompt, trace)

        # 2) Moderation (hết giờ -> không phát)
        stamp(trace, "t4_moderation_end")
        ok, safe = moderate(raw)
        if not ok:
            _log.info(f"blocked turn={turn_id}"); self.state = State.IDLE; return

        # 3) Cắt câu -> phát sang Node3 (KHÔNG còn persona-rewrite)
        mood = Mood.NEUTRAL
        sents = split_sentences(safe)
        self.state = State.SPEAKING; self._speaking_turn = turn_id
        for i, s in enumerate(sents):
            out = SentenceOut(turn_id, i, s, i == len(sents) - 1, mood)
            self.egress.send_sentence(out, trace if i == 0 else None)
        self.history.append(session, f"user: {rec.content}\nai: {safe}")

    def _build_prompt(self, session, content):
        ctx = self.history.context(session)
        return f"{ctx}\nuser: {content}\nai:" if ctx else f"user: {content}\nai:"

    def _call_llm(self, prompt, trace) -> str:
        for attempt in (1, 2):
            try:
                stamp(trace, "t2_llm_start")
                out = "".join(self.llm.stream(prompt, self._cancel_check))
                stamp(trace, "t3_llm_end")
                if out.strip(): return out
            except Exception as e:
                _log.info(f"llm attempt {attempt} fail: {e}")
        stamp(trace, "t3_llm_end")
        return fallback_sentence()