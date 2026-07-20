from node4_dashboard.state import DashState

def test_state_apply():
    st = DashState()
    st.apply({"kind":"state","data":{"state":"SPEAKING"}})
    st.apply({"kind":"queue","data":{"size":3}})
    st.apply({"kind":"chat","data":{"user":"An","content":"chào Mai"}})
    st.apply({"kind":"crisis","data":{"content":"tôi muốn chết"}})
    snap = st.snapshot()
    assert snap["state"] == "SPEAKING"
    assert snap["queue"] == 3
    assert "An: chào Mai" in snap["chats"]
    assert snap["crises"] == ["tôi muốn chết"]

def test_emitter_never_blocks(monkeypatch):
    # Node4 chết (không ai nghe) -> emit KHÔNG được raise/chặn
    from node2_core.events import DashboardEmitter
    em = DashboardEmitter(maxlen=2)
    for _ in range(100):         # spam quá maxlen -> phải bỏ, không treo
        em.emit.__self__._q.put_nowait if False else None
    from shared.contracts.events import EventKind
    for _ in range(100): em.emit(EventKind.CHAT, x=1)   # không raise là đạt

def test_skip_uses_stopsignal():
    from node2_core.orchestrator import Orchestrator
    class RecEgress:
        def __init__(self): self.stops=[]
        def send_sentence(self,s,trace=None): pass
        def send_stop(self,t): self.stops.append(t)
    o = Orchestrator(egress=RecEgress(), llm=type("L",(),{"stream":lambda s,m,c:iter([])})(),
                     crisis=type("C",(),{"is_crisis":lambda s,c:False})(),
                     health=type("H",(),{"is_healthy":lambda s:True})())
    o._speaking_turn = "turn-X"
    o.skip()                     # skip turn hiện tại
    assert o.egress.stops == ["turn-X"]    # dùng đúng StopSignal