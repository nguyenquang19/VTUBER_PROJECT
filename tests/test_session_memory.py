from node2_core.memory.session_memory import SessionMemory

class FakeLLM:
    def stream(self, msgs, cc):
        yield "Tóm tắt: người xem chào hỏi và hỏi về game."

def test_context_includes_summary_and_recent():
    m = SessionMemory(budget=200, llm=FakeLLM())
    for i in range(20):
        m.append("s1", f"user: tin {i}\nai: trả lời {i}")
    m.maybe_summarize("s1")
    import time; time.sleep(0.3)          # chờ tóm tắt nền
    ctx = m.context("s1")
    assert "Tóm tắt" in ctx               # có tóm tắt đầu buổi
    assert "tin 19" in ctx                # vẫn giữ tin gần nhất

def test_fallback_when_no_llm():
    m = SessionMemory(budget=100, llm=None)   # không LLM -> fallback cắt bớt
    for i in range(20):
        m.append("s1", f"dòng {i}")
    m.maybe_summarize("s1")
    import time; time.sleep(0.2)
    ctx = m.context("s1")
    assert ctx                             # có nội dung, không crash