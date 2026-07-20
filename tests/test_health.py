from node2_core.health.llm_health import LLMHealthMonitor

def test_probe_down_when_no_server():
    # Không có server ở cổng lạ -> probe phải trả False
    m = LLMHealthMonitor(host="http://127.0.0.1:59999", interval=999, timeout=0.5)
    assert m._probe() is False

def test_fallback_when_down(monkeypatch):
    from node2_core.orchestrator import Orchestrator
    from shared.utils.mocks import mock_record
    class FakeHealth:
        def is_healthy(self): return False      # giả lập LLM chết
    class RecEgress:
        def __init__(self): self.sents=[]
        def send_sentence(self,s,trace=None): self.sents.append(s)
        def send_stop(self,t): pass
    class NeverLLM:
        def stream(self,m,cc):
            raise AssertionError("KHÔNG được gọi LLM khi health DOWN")
    eg = RecEgress()
    o = Orchestrator(egress=eg, llm=NeverLLM(), crisis=type("C",(),{"is_crisis":lambda s,c:False})(),
                     health=FakeHealth())
    o.submit(mock_record("chào")); o.run_once()
    # phải phát fallback, KHÔNG gọi LLM (NeverLLM sẽ raise nếu bị gọi)
    assert eg.sents, "phải có câu fallback"