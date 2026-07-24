from node2_core.orchestrator import Orchestrator
from shared.contracts.payloads import IngestionRecord, EventType, Priority
from shared.contracts.timing import TimingTrace
from shared.utils.mocks import mock_record


class FakeLLM:
    def __init__(self, text): self.text = text
    def stream(self, prompt, cc):
        for c in self.text:
            if cc(): return
            yield c


class HealthyStub:
    def is_healthy(self): return True


class RecEgress:
    def __init__(self): self.sents = []; self.stops = []
    def send_sentence(self, s, trace=None): self.sents.append(s)
    def send_stop(self, t): self.stops.append(t)


class NoNode5:
    def get_profile(self, s): return None


def _auto_rec(facts):
    return IngestionRecord(EventType.AUTO, "__auto__", "Mai", "", 0.0, 0.0,
                           TimingTrace(turn_id="auto-1"), facts)


def _orch(llm):
    return Orchestrator(egress=RecEgress(), llm=llm,
                        crisis=type("C", (), {"is_crisis": lambda s, c: False})(),
                        health=HealthyStub(), node5=NoNode5())


def test_auto_record_routes_to_auto_priority():
    o = _orch(FakeLLM("x"))
    o.submit(_auto_rec({"chat_silent_sec": 300}))
    pri, rec = o.inbox.get()
    assert pri == Priority.AUTO


def test_auto_turn_emits_sentence():
    o = _orch(FakeLLM("Ơ ông vẫn đấy chứ."))
    o.submit(_auto_rec({"mai_silent_sec": 300}))
    o.run_once()
    assert any("ông vẫn đấy" in s.text.lower() for s in o.egress.sents)


def test_auto_prompt_contains_facts_and_idle_request():
    o = _orch(FakeLLM("hi"))
    facts = {"voice_members": ["Quang", "Linh"], "chat_silent_sec": 240}
    msgs = o._build_prompt("__auto__", "", facts)
    joined = " ".join(m["content"] for m in msgs)
    assert "Quang" in joined and "Linh" in joined         # dữ kiện có trong prompt
    assert msgs[-1]["role"] == "user"
    assert "Đang rảnh" in msgs[-1]["content"]              # khối yêu cầu kiểu tự nói


def test_chat_interrupts_ongoing_auto():
    o = _orch(FakeLLM("hi"))
    o._speaking_turn = "auto-running"
    o._speaking_is_auto = True
    o.submit(mock_record("chào Mai", user_id="u1"))       # chat thường tới
    assert "auto-running" in o.egress.stops               # cắt lượt tự nói


def test_chat_does_not_interrupt_ongoing_reply():
    o = _orch(FakeLLM("hi"))
    o._speaking_turn = "reply-running"
    o._speaking_is_auto = False
    o.submit(mock_record("chào", user_id="u1"))
    assert o.egress.stops == []                            # chat KHÔNG cắt lượt trả lời (§7)
