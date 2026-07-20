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
from .events import emit_event
from shared.contracts.events import EventKind
_log = get_logger("node2")


def _safe_sum(*vals):
    xs = [v for v in vals if v is not None]
    return round(sum(xs), 1) if xs else None


class Orchestrator:
    def __init__(self, egress=None, llm=None, crisis=None, health=None, node5=None):
        self.state = State.IDLE
        self.inbox = PriorityInbox()
        self.llm = llm or LlamaCppClient()
        from .memory.session_memory import SessionMemory
        self.history = SessionMemory(llm=self.llm)
        self.egress = egress or Node3Egress()
        self._speaking_turn = None
        if crisis is None:
            from .crisis.detector import RuleBasedCrisisDetector
            crisis = RuleBasedCrisisDetector()
        self.crisis = crisis
        self.health = health   # None = không giám sát (test/dev)
        if health is None:
            from .health.llm_health import LLMHealthMonitor
            self.health = LLMHealthMonitor()
            self.health.start()
        from .node5_client import Node5Client
        self.node5 = node5 if node5 is not None else Node5Client()

        import time, threading
        self._last_activity = {}          # session -> timestamp cuối
        self._session_ids = {}            # session -> id buổi (chống ghi trùng)
        # job nền quét session im lặng -> tóm tắt hồ sơ
        threading.Thread(target=self._session_flush_loop, daemon=True).start()

        self._display_names = {}

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
        print(f'HANDLE_COUNT {rec.timing.turn_id[:8]}', flush=True)
        import time
        trace: TimingTrace = rec.timing
        turn_id = trace.turn_id
        session = rec.user_id
        self.state = (State.CRISIS_PRIORITY if pri == Priority.CRISIS
                      else State.PROCESSING)

        emit_event(EventKind.CHAT, user=rec.display_name, content=rec.content)
        emit_event(EventKind.STATE, state=self.state.name)
        emit_event(EventKind.QUEUE, size=self.inbox.get_size())
        if pri == Priority.CRISIS:
            emit_event(EventKind.CRISIS, content=rec.content)
        self._display_names[rec.user_id] = rec.display_name

        # 1) LLM
        messages = self._build_prompt(session, rec.content)
        raw = self._call_llm(messages, trace)

        # 2) Moderation -> gan `safe` TRUOC khi dung
        stamp(trace, "t4_moderation_end")
        ok, safe = moderate(raw)
        if not ok:
            _log.info(f"blocked turn={turn_id}")
            self.state = State.IDLE
            emit_event(EventKind.STATE, state=self.state.name)
            return

        emit_event(EventKind.REPLY, text=safe)

        # 3) Cat cau -> phat
        mood = Mood.NEUTRAL
        sents = split_sentences(safe)
        if not sents:
            self.state = State.IDLE
            return

        self.state = State.SPEAKING
        self._speaking_turn = turn_id
        emit_event(EventKind.STATE, state=self.state.name)
        for i, s in enumerate(sents):
            out = SentenceOut(turn_id, i, s, i == len(sents) - 1, mood)
            self.egress.send_sentence(out, trace if i == 0 else None)

        # 4) Luu ngu canh
        self.history.append(session, f"user: {rec.content}\nai: {safe}")
        self.history.maybe_summarize(session)
        self._last_activity[session] = time.time()

        emit_partial("node2", trace)
        d = trace.deltas()
        emit_event(EventKind.TIMING, deltas={
            "llm_call": d.get("llm_call"),
            "moderation": d.get("moderation"),
            "queue_wait": d.get("queue_wait"),
            "node2_total": _safe_sum(d.get("queue_wait"), d.get("llm_call"), d.get("moderation")),
        })

        self._speaking_turn = None
        self.state = State.IDLE
        emit_event(EventKind.STATE, state=self.state.name)

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
        if self.health and not self.health.is_healthy():
            _log.info("LLM DOWN (healthcheck) -> fallback ngay")
            stamp(trace, "t2_llm_start"); stamp(trace, "t3_llm_end")
            return fallback_sentence()
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
    
    def skip(self, turn_id: str = ""):
        # Skip = dừng turn đang nói. Rỗng -> turn hiện tại.
        target = turn_id or self._speaking_turn
        if target:
            self.egress.send_stop(target)     # ĐÚNG đường StopSignal đã xây cho crisis
            emit_event(EventKind.STATE, state="SKIPPED")

    def _build_prompt(self, session, content):
        msgs = [{"role": "system", "content":
                 "Bạn là VTuber AI tên Mai, vui vẻ thân thiện. "
                 "Trả lời bằng tiếng Việt, TỐI ĐA 2 câu ngắn, tự nhiên. "
                 "Không dài dòng, không nhắc mình là AI."}]

        # Node5: chèn hồ sơ người quen NẾU lấy được trong 300ms. Lỗi -> bỏ qua.
        if self.node5:
            prof = self.node5.get_profile(session)   # session = user_id
            if prof and prof.get("summary"):
                msgs.append({"role": "system", "content":
                             f"Bạn từng nói chuyện với người này. Ghi nhớ: {prof['summary']}"})

        ctx = self.history.context(session)
        if ctx:
            msgs.append({"role": "system", "content": f"Ngữ cảnh buổi này:\n{ctx}"})
        msgs.append({"role": "user", "content": content})
        return msgs

    def _session_id_for(self, session):
        # id buổi ổn định cho idempotent; đổi khi buổi mới bắt đầu sau khi flush
        import uuid
        if session not in self._session_ids:
            self._session_ids[session] = str(uuid.uuid4())
        return self._session_ids[session]

    def _session_flush_loop(self, idle_sec=300, check_every=60):
        import time
        while True:
            time.sleep(check_every)
            now = time.time()
            stale = [s for s, t in list(self._last_activity.items())
                     if now - t > idle_sec]
            for session in stale:
                self._flush_session(session)

    def _flush_session(self, session):
        # Tóm tắt hồ sơ NGƯỜI ĐANG CHAT (session=user_id của họ). KHÔNG ghi người thứ 3.
        if not self.node5:
            return
        summary = self.history.context(session)[:400]   # tóm tắt buổi cho hồ sơ
        if not summary.strip():
            return
        display = self._display_names.get(session, session)
        sid = self._session_id_for(session)
        # chạy nền, idempotent theo (user, session) phía Node5
        import threading
        threading.Thread(target=self.node5.write_summary,
                         args=(session, display, sid, summary), daemon=True).start()
        # dọn để buổi sau bắt đầu session mới
        self._last_activity.pop(session, None)
        self._session_ids.pop(session, None)

    def flush_all_sessions(self):
        for session in list(self._last_activity.keys()):
            self._flush_session(session)