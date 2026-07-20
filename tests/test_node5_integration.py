from node2_core.orchestrator import Orchestrator
from shared.utils.mocks import mock_record

class FakeNode5:
    def __init__(self): self.written = []; self.profiles = {}
    def get_profile(self, uid): return self.profiles.get(uid)
    def write_summary(self, uid, name, sid, summary):
        self.written.append((uid, name, sid, summary)); return {"status":"written"}

class FakeLLM:
    def stream(self, m, cc):
        for c in "Chào bạn nhé.": yield c

class RecEgress:
    def send_sentence(self, s, trace=None): pass
    def send_stop(self, t): pass

def _orch(n5):
    return Orchestrator(egress=RecEgress(), llm=FakeLLM(),
                        crisis=type("C",(),{"is_crisis":lambda s,c:False})(),
                        health=type("H",(),{"is_healthy":lambda s:True})(),
                        node5=n5)

def test_profile_injected_into_prompt():
    n5 = FakeNode5()
    n5.profiles["u1"] = {"summary": "An thích game bắn súng."}
    o = _orch(n5)
    msgs = o._build_prompt("u1", "chào Mai")
    joined = " ".join(m["content"] for m in msgs)
    assert "An thích game" in joined      # hồ sơ được chèn

def test_no_profile_still_works():
    n5 = FakeNode5()                       # không có hồ sơ u2
    o = _orch(n5)
    msgs = o._build_prompt("u2", "chào")
    assert msgs                            # vẫn build được prompt, không crash

def test_flush_writes_only_chatter():
    n5 = FakeNode5()
    o = _orch(n5)
    o._display_names["u1"] = "An"
    o.history.append("u1", "user: chào\nai: chào bạn")
    o._last_activity["u1"] = 0             # giả lập đã lâu
    o._flush_session("u1")
    import time; time.sleep(0.2)
    # chỉ ghi hồ sơ u1 (người chat), đúng session
    assert len(n5.written) == 1
    assert n5.written[0][0] == "u1"