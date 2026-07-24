from node1_ingestion.room_state import RoomStatePublisher
from node6_idle.facts import read_room_state


class Pulse:
    def snapshot(self): return {"msgs_30s": 5, "active_users_30s": 2}


class BadPulse:
    def snapshot(self): raise RuntimeError("hỏng")


def test_roundtrip_node1_publishes_node6_reads(tmp_path):
    p = str(tmp_path / "room_state.json")
    pub = RoomStatePublisher(Pulse(), path=p)
    pub.note_chat()
    pub.write_once()
    got = read_room_state(p)
    assert got["room_pulse"]["msgs_30s"] == 5
    assert got["chat_silent_sec"] is not None and got["chat_silent_sec"] < 5
    assert got["voice_members"] == []
    assert got["someone_speaking"] is False


def test_no_chat_yet_means_silent_none(tmp_path):
    p = str(tmp_path / "rs.json")
    pub = RoomStatePublisher(Pulse(), path=p)
    pub.write_once()                       # chưa note_chat lần nào
    assert read_room_state(p)["chat_silent_sec"] is None


def test_bad_pulse_does_not_crash_publisher(tmp_path):
    p = str(tmp_path / "rs.json")
    pub = RoomStatePublisher(BadPulse(), path=p)
    pub.write_once()                       # KHÔNG được ném
    assert read_room_state(p)["room_pulse"] == {}


def test_missing_file_returns_empty():
    assert read_room_state("/khong/ton/tai/rs.json") == {}
