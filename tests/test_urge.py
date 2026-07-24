from node6_idle.urge import compute_urge, compute_threshold, should_speak
from node6_idle import config as C


class NoNoise:
    def uniform(self, a, b): return 0.0     # tắt nhiễu ngẫu nhiên để test tất định


# ---------- urge ----------

def test_someone_speaking_kills_urge():
    assert compute_urge({"someone_speaking": True, "chat_silent_sec": 9999}) == 0.0


def test_chat_silence_raises_urge():
    low = compute_urge({"chat_silent_sec": 30, "mai_silent_sec": 999})
    high = compute_urge({"chat_silent_sec": 600, "mai_silent_sec": 999})
    assert high > low


def test_just_spoke_damps_urge():
    just = compute_urge({"chat_silent_sec": 300, "mai_silent_sec": 5})
    long_ago = compute_urge({"chat_silent_sec": 300, "mai_silent_sec": 999})
    assert just < long_ago


def test_just_joined_jumps():
    base = compute_urge({"chat_silent_sec": 0, "mai_silent_sec": 999})
    joined = compute_urge({"chat_silent_sec": 0, "mai_silent_sec": 999,
                           "just_joined": ["Linh"]})
    assert joined - base >= C.URGE_JUST_JOINED_BONUS - 0.01


def test_busy_room_lowers_urge():
    calm = compute_urge({"chat_silent_sec": 300, "mai_silent_sec": 999})
    busy = compute_urge({"chat_silent_sec": 300, "mai_silent_sec": 999,
                         "room_pulse": {"msgs_30s": 20}})
    assert busy < calm


# ---------- threshold: mood điều khiển ----------

def test_bon_chon_lowers_threshold():
    calm = compute_threshold({"bon_chon": 0}, rng=NoNoise())
    antsy = compute_threshold({"bon_chon": 8}, rng=NoNoise())
    assert antsy < calm


def test_buon_raises_threshold():
    calm = compute_threshold({"buon": 0}, rng=NoNoise())
    sad = compute_threshold({"buon": 8}, rng=NoNoise())
    assert sad > calm


def test_threshold_has_floor():
    th = compute_threshold({"bon_chon": 10, "vui": 10}, rng=NoNoise())
    assert th >= C.THRESHOLD_MIN


# ---------- should_speak ----------

def test_should_speak_true_when_long_silence():
    facts = {"chat_silent_sec": 9999, "mai_silent_sec": 9999}
    assert should_speak(facts, {"bon_chon": 5}, rng=NoNoise()) is True


def test_should_speak_false_when_voice_active():
    assert should_speak({"someone_speaking": True}, {"bon_chon": 10}, rng=NoNoise()) is False
