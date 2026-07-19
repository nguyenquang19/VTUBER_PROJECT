from node2_core.orchestrator import Orchestrator
from node2_core.crisis_stub import CrisisDetector
from shared.utils.mocks import mock_record

class KeywordCrisis:  # giả lập GĐ5 để test đường crisis
    def is_crisis(self, content): return "khủng hoảng" in content.lower()

class FakeLLM:
    def stream(self, prompt, cancel_check):
        for w in ["Chào bạn. ", "Rất vui được nói chuyện. ", "Hẹn gặp lại."]:
            if cancel_check(): return
            yield w

if __name__ == "__main__":
    o = Orchestrator(llm=FakeLLM(), crisis=KeywordCrisis())
    o.submit(mock_record("xin chào"))
    o.run_once()
    o.submit(mock_record("tôi đang khủng hoảng", user_id="u2"))
    o.run_once()
