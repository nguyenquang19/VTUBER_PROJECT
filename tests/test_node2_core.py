from node2_core.orchestrator import Orchestrator
from node2_core.queue import PriorityInbox
from node2_core.splitter import split_sentences
from shared.contracts.payloads import Priority
from shared.utils.mocks import mock_record

class FakeLLM:
    def __init__(self, text="Câu một. Câu hai."): self.text = text
    def stream(self, prompt, cancel_check):
        for c in self.text:
            if cancel_check(): return
            yield c

class RecordEgress:
    def __init__(self): self.sents = []; self.stops = []
    def send_sentence(self, s, trace=None): self.sents.append(s)
    def send_stop(self, turn_id): self.stops.append(turn_id)

def test_priority_order():
    q = PriorityInbox()
    q.put(Priority.AUTO, "a"); q.put(Priority.CRISIS, "c"); q.put(Priority.CHAT, "b")
    assert [q.get()[0] for _ in range(3)] == [Priority.CRISIS, Priority.CHAT, Priority.AUTO]

def test_splitter():
    assert split_sentences("A. B! C?") == ["A.", "B!", "C?"]

def test_normal_turn_emits_sentences():
    eg = RecordEgress()
    o = Orchestrator(egress=eg, llm=FakeLLM("Xin chào. Tạm biệt."))
    o.submit(mock_record("hi")); o.run_once()
    assert [s.text for s in eg.sents] == ["Xin chào.", "Tạm biệt."]
    assert eg.sents[-1].is_last is True

def test_crisis_interrupts_speaking():
    # Giả lập đang phát turn A, crisis B tới -> stop A gửi ngay, không đợi.
    eg = RecordEgress()
    class C:
        def is_crisis(self, c): return "cứu" in c
    o = Orchestrator(egress=eg, llm=FakeLLM(), crisis=C())
    o._speaking_turn = "turn-A"           # giả lập đang nói
    o.submit(mock_record("cứu tôi với", user_id="u9"))
    assert eg.stops == ["turn-A"]         # stop gửi tức thì lúc submit, không chờ run_once