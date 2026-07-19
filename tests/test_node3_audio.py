from node3_audio.node3 import Node3
from node3_audio.cancel import CancelRegistry
from node3_audio.subtitle import _ts
from shared.contracts.payloads import SentenceOut, Mood

class FakeTTS:
    def __init__(self): self.calls = []
    def synth(self, text, wav_path):
        self.calls.append((text, wav_path)); open(wav_path, "wb").close()
        return 500.0

def _s(turn, seq, text, last):
    return SentenceOut(turn, seq, text, last, Mood.NEUTRAL)

def test_vtt_timestamp():
    assert _ts(3661500) == "01:01:01.500"

def test_normal_turn_synths_all(tmp_path):
    from node3_audio.player import FilePlayer
    tts = FakeTTS(); n = Node3(tts=tts, player=FilePlayer(str(tmp_path)))
    n.on_sentence(_s("t1", 0, "A.", False)); n.on_sentence(_s("t1", 1, "B.", True))
    assert [c[0] for c in tts.calls] == ["A.", "B."]

def test_stop_skips_rest_of_turn(tmp_path):
    from node3_audio.player import FilePlayer
    tts = FakeTTS(); n = Node3(tts=tts, player=FilePlayer(str(tmp_path)))
    n.on_sentence(_s("t1", 0, "A.", False))   # phát
    n.stop("t1")                               # stop giữa turn
    n.on_sentence(_s("t1", 1, "B.", True))     # phải bị bỏ
    assert [c[0] for c in tts.calls] == ["A."]  # chỉ A, không có B

def test_stop_does_not_affect_other_turn(tmp_path):
    from node3_audio.player import FilePlayer
    tts = FakeTTS(); n = Node3(tts=tts, player=FilePlayer(str(tmp_path)))
    n.stop("t1")
    n.on_sentence(_s("t2", 0, "X.", True))     # turn khác -> vẫn phát
    assert [c[0] for c in tts.calls] == ["X."]