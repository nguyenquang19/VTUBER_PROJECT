from node2_core.memory.session_memory import SessionMemory
from node2_core.orchestrator import Orchestrator, MAI_CHARACTER_PROMPT
from node2_core.config import CTX_BUDGET_CHARS
from shared.utils.mocks import mock_record


class FakeLLM:
    def __init__(self, text="Câu một."): self.text = text
    def stream(self, prompt, cancel_check):
        for c in self.text:
            if cancel_check(): return
            yield c


class HealthyStub:
    def is_healthy(self): return True


class NoNode5:
    def get_profile(self, session): return None


def _orch():
    return Orchestrator(egress=type("E", (), {"send_sentence": lambda s, x, trace=None: None,
                                              "send_stop": lambda s, t: None})(),
                        llm=FakeLLM(), crisis=type("C", (), {"is_crisis": lambda s, c: False})(),
                        health=HealthyStub(), node5=NoNode5())


# ---------- context_within ----------

def test_context_within_never_exceeds_max():
    sm = SessionMemory(budget=100000)
    for i in range(200):
        sm.append("s", f"user: tin dài số {i} " + "x" * 40)
    out = sm.context_within("s", 500)
    assert len(out) <= 500


def test_context_within_keeps_newest_drops_oldest():
    sm = SessionMemory(budget=100000)
    sm.append("s", "LƯỢT_CŨ_NHẤT " + "x" * 60)
    sm.append("s", "LƯỢT_GIỮA " + "x" * 60)
    sm.append("s", "LƯỢT_MỚI_NHẤT " + "x" * 60)
    out = sm.context_within("s", 90)          # chỉ đủ ~1 lượt
    assert "LƯỢT_MỚI_NHẤT" in out
    assert "LƯỢT_CŨ_NHẤT" not in out


def test_context_within_summary_has_priority_over_recent():
    sm = SessionMemory(budget=100000)
    sm._summary["s"] = "tóm tắt buổi này rất quan trọng"
    sm.append("s", "một lượt gần đây " + "y" * 200)
    out = sm.context_within("s", 60)
    assert "tóm tắt buổi này" in out           # tóm tắt được giữ
    assert "y" * 200 not in out                # lượt dài bị bỏ vì hết chỗ


def test_context_within_zero_budget_returns_empty():
    sm = SessionMemory(budget=100000)
    sm.append("s", "gì đó")
    assert sm.context_within("s", 0) == ""


def test_context_within_chronological_order_preserved():
    sm = SessionMemory(budget=100000)
    sm.append("s", "AAA")
    sm.append("s", "BBB")
    sm.append("s", "CCC")
    out = sm.context_within("s", 1000)
    assert out.index("AAA") < out.index("BBB") < out.index("CCC")


# ---------- ngân sách thật trong _build_prompt ----------

def test_build_prompt_total_within_budget_even_with_huge_history():
    o = _orch()
    for i in range(500):
        o.history.append("u1", f"user: nói chuyện phiếm số {i} " + "z" * 50)
    msgs = o._build_prompt("u1", "câu hỏi mới")
    total = sum(len(m["content"]) for m in msgs)
    assert total <= CTX_BUDGET_CHARS


def test_build_prompt_never_drops_character_prompt():
    o = _orch()
    for i in range(500):
        o.history.append("u1", "x" * 100)     # lịch sử khổng lồ
    msgs = o._build_prompt("u1", "hi")
    assert msgs[0]["content"] == MAI_CHARACTER_PROMPT   # nhân vật luôn ở đầu, nguyên vẹn


def test_build_prompt_reminder_at_end_before_user():
    o = _orch()
    msgs = o._build_prompt("u1", "chào Mai")
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"] == "chào Mai"
    reminder = msgs[-2]["content"]             # dòng nhắc ngay trước user
    assert "tớ" in reminder and "vui:N" in reminder   # nhắc nhân vật + định dạng mood
