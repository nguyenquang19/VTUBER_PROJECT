import os
import tempfile
import time

from node2_core.mood import MoodState, MoodStore
from node2_core.mood_parser import split_reply_and_mood


PREV = MoodState(vui=3, buon=5, buc=2, bon_chon=7, nguong=0,
                  ly_do="gọi ông mấy lần không ai đáp", updated_at=time.time() - 360)


def test_parser_accepts_diacritic_dim_names_from_character_prompt():
    # Bộ nhân vật dạy model in CÓ DẤU — parser phải nhận được, không phải
    # chỉ chấp nhận bản ASCII trong ví dụ của DAC-TA-KY-THUAT.md
    raw = (
        "Ơ ông đây rồi.\n\n"
        "[vui:5 buồn:3 bực:2 bồn_chồn:4 ngượng:0]\n"
        "lý do: ông vẫn im nhưng có Linh nói chuyện nên đỡ trống\n"
        "còn nữa: không"
    )
    reply, mood, con_nua = split_reply_and_mood(raw, PREV)
    assert mood.vui == 5 and mood.buon == 3 and mood.buc == 2
    assert mood.bon_chon == 4 and mood.nguong == 0


def test_parser_full_format():
    raw = (
        "Thôi kệ ông. Linh ơi con mèo đỡ chưa, hôm nọ nghe bảo ốm.\n\n"
        "[vui:5 buon:3 buc:2 bon_chon:4 nguong:0]\n"
        "lý do: ông vẫn im nhưng có Linh nói chuyện nên đỡ trống\n"
        "còn nữa: không"
    )
    reply, mood, con_nua = split_reply_and_mood(raw, PREV)
    assert reply == "Thôi kệ ông. Linh ơi con mèo đỡ chưa, hôm nọ nghe bảo ốm."
    assert mood.vui == 5 and mood.buon == 3 and mood.buc == 2
    assert mood.bon_chon == 4 and mood.nguong == 0
    assert "đỡ trống" in mood.ly_do
    assert con_nua is False


def test_parser_con_nua_co():
    raw = "Khoan đã tớ kể tiếp\n[vui:6 buon:0 buc:0 bon_chon:1 nguong:0]\nlý do: đang vui\ncòn nữa: có"
    reply, mood, con_nua = split_reply_and_mood(raw, PREV)
    assert con_nua is True


def test_parser_missing_mood_block_keeps_prev():
    raw = "Ơ ông đi đâu vậy, quên tớ luôn à."
    reply, mood, con_nua = split_reply_and_mood(raw, PREV)
    assert reply == raw.strip()
    assert mood is PREV
    assert con_nua is False


def test_parser_missing_one_dimension_falls_back_to_prev():
    raw = "Ừ thì thôi.\n[vui:8 buc:1 bon_chon:2 nguong:1]\nlý do: có người mới vào"
    reply, mood, con_nua = split_reply_and_mood(raw, PREV)
    assert mood.vui == 8
    assert mood.buon == PREV.buon    # thiếu -> lấy từ prev
    assert mood.buc == 1


def test_parser_extra_trailing_text_ignored_in_reply():
    raw = "Câu nói của Mai.\n[vui:2 buon:2 buc:2 bon_chon:2 nguong:2]\nlý do: bình thường\ncòn nữa: không\nrác thừa phía sau"
    reply, mood, con_nua = split_reply_and_mood(raw, PREV)
    assert reply == "Câu nói của Mai."


def test_moodstore_set_then_get():
    with tempfile.TemporaryDirectory() as d:
        store = MoodStore(db_path=os.path.join(d, "mood.db"),
                          log_path=os.path.join(d, "mood-history.jsonl"))
        new_mood = MoodState(vui=7, buon=1, buc=0, bon_chon=2, nguong=0,
                              ly_do="test", updated_at=time.time())
        store.set(new_mood, turn_id="t1", said="chào")
        got = store.get()
        assert got.vui == 7 and got.ly_do == "test"
        store.close()   # flush thread nền trước khi xóa thư mục tạm


def test_moodstore_persists_to_sqlite_across_instances():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "mood.db")
        log_path = os.path.join(d, "mood-history.jsonl")
        store1 = MoodStore(db_path=db_path, log_path=log_path)
        mood = MoodState(vui=9, buon=0, buc=0, bon_chon=0, nguong=0,
                          ly_do="siêu vui", updated_at=time.time())
        store1.set(mood, turn_id="t2", said="yay")
        store1.close()    # đợi thread nền ghi xong (không cần sleep đoán mò)

        store2 = MoodStore(db_path=db_path, log_path=log_path)
        got = store2.get()
        assert got.vui == 9
        assert got.ly_do == "siêu vui"
        store2.close()


def test_moodstore_default_when_no_db():
    with tempfile.TemporaryDirectory() as d:
        store = MoodStore(db_path=os.path.join(d, "khong-ton-tai.db"),
                          log_path=os.path.join(d, "log.jsonl"))
        got = store.get()
        assert got == MoodState()
        store.close()


def test_moodstore_age_seconds():
    with tempfile.TemporaryDirectory() as d:
        store = MoodStore(db_path=os.path.join(d, "mood.db"),
                          log_path=os.path.join(d, "log.jsonl"))
        assert store.age_seconds() == 0.0   # chưa có mood nào
        mood = MoodState(vui=1, buon=0, buc=0, bon_chon=0, nguong=0,
                          ly_do="x", updated_at=time.time() - 120)
        store.set(mood)
        age = store.age_seconds()
        assert 115 < age < 125
        store.close()