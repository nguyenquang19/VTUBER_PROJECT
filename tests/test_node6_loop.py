import time

import node6_idle.node6 as node6mod
from node6_idle.node6 import Node6State, tick
from node6_idle.facts import build_facts, read_mood
from node6_idle import config as C


# ---------- phanh an toàn ----------

def test_brake_stops_at_max_auto_streak(monkeypatch):
    sent = []
    monkeypatch.setattr(node6mod, "read_mood", lambda: {"updated_at": 0})
    monkeypatch.setattr(node6mod, "read_room_state", lambda: {"chat_silent_sec": 9999})
    monkeypatch.setattr(node6mod, "should_speak", lambda f, m: True)
    monkeypatch.setattr(node6mod, "send_auto_request", lambda facts: sent.append(facts))
    st = Node6State()
    for _ in range(20):
        tick(st, time.time())
    assert len(sent) == C.MAX_AUTO_STREAK   # phanh chặn đúng ngưỡng, không nói mãi


def test_new_chat_resets_streak(monkeypatch):
    sent = []
    monkeypatch.setattr(node6mod, "read_mood", lambda: {"updated_at": 0})
    monkeypatch.setattr(node6mod, "read_room_state", lambda: {"chat_silent_sec": 0})
    monkeypatch.setattr(node6mod, "should_speak", lambda f, m: True)
    monkeypatch.setattr(node6mod, "send_auto_request", lambda facts: sent.append(facts))
    st = Node6State()
    for _ in range(20):
        tick(st, time.time())
    assert len(sent) == 20   # chat mới mỗi tick -> streak reset -> không chạm phanh


def test_tick_no_speak_when_should_speak_false(monkeypatch):
    sent = []
    monkeypatch.setattr(node6mod, "read_mood", lambda: {"updated_at": 0})
    monkeypatch.setattr(node6mod, "read_room_state", lambda: {})
    monkeypatch.setattr(node6mod, "should_speak", lambda f, m: False)
    monkeypatch.setattr(node6mod, "send_auto_request", lambda facts: sent.append(facts))
    st = Node6State()
    for _ in range(5):
        tick(st, time.time())
    assert sent == []


# ---------- facts ----------

def test_mai_silent_from_mood_updated_at():
    st = Node6State()
    f = build_facts(st, now=1000.0, mood={"updated_at": 940.0}, room={})
    assert 59 <= f["mai_silent_sec"] <= 61


def test_facts_safe_defaults_when_room_empty():
    st = Node6State()
    f = build_facts(st, 1000.0, {"updated_at": 0}, {})
    assert f["voice_members"] == []
    assert f["someone_speaking"] is False
    assert f["auto_streak"] == 0


def test_read_mood_missing_file_returns_zero(tmp_path):
    m = read_mood(str(tmp_path / "khong-co.db"))
    assert m["vui"] == 0 and m["updated_at"] == 0.0


def test_read_mood_reads_what_node2_wrote(tmp_path):
    # Hợp đồng dùng-chung mood giữa node2 (ghi) và node6 (đọc) — he-thong-logic §3
    from node2_core.mood import MoodStore, MoodState
    db = str(tmp_path / "mood.db")
    store = MoodStore(db_path=db, log_path=str(tmp_path / "l.jsonl"))
    store.set(MoodState(vui=7, bon_chon=4, ly_do="x", updated_at=time.time()))
    store.close()
    m = read_mood(db)
    assert m["vui"] == 7 and m["bon_chon"] == 4
