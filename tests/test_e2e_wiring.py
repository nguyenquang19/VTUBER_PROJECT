# Node1->Node2->Node3 in-process, dùng LLM giả + TTS giả. Không mạng, không Discord.
from node2_core.orchestrator import Orchestrator
from node3_audio.node3 import Node3
from node3_audio.player import FilePlayer
from shared.contracts.payloads import SentenceOut, Mood
from shared.utils.mocks import mock_record

class FakeLLM:
    def stream(self, prompt, cancel_check):
        for c in "Xin chào bạn. Hẹn gặp lại.":
            if cancel_check(): return
            yield c

class FakeTTS:
    def __init__(self): self.texts = []
    def synth(self, text, wav_path):
        self.texts.append(text); open(wav_path, "wb").close(); return 300.0

def test_wiring_node2_to_node3(tmp_path):
    tts = FakeTTS(); n3 = Node3(tts=tts, player=FilePlayer(str(tmp_path)))
    # egress giả: chuyển thẳng SentenceOut từ Node2 sang Node3 (thay TCP)
    class DirectEgress:
        def send_sentence(self, s, trace=None): n3.on_sentence(s, trace)
        def send_stop(self, turn_id): n3.stop(turn_id)
    o = Orchestrator(egress=DirectEgress(), llm=FakeLLM())
    o.submit(mock_record("hello")); o.run_once()
    # Node2 cắt câu -> Node3 synth đủ 2 câu, tạo .wav
    assert tts.texts == ["Xin chào bạn.", "Hẹn gặp lại."]