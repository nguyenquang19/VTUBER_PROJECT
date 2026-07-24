import time
from node1_ingestion.noise_filter import is_noise, RoomPulse


class TestIsNoise:
    def test_too_short(self):
        assert is_noise("ok") is True
        assert is_noise("  a  ") is True
        assert is_noise("") is True

    def test_emoji_or_symbols_only(self):
        assert is_noise("😂😂😂") is True
        assert is_noise("👍") is True
        assert is_noise("...") is True
        assert is_noise("???") is True

    def test_short_reaction_list(self):
        for w in ["ừ", "uh", "ok", "oke", "haha", "hihi", "hehe",
                  "kkk", "vâng", "dạ", "=))"]:
            assert is_noise(w) is True
            assert is_noise(w.upper()) is True

    def test_repeated_char(self):
        assert is_noise("aaaaa") is True
        assert is_noise("!!!!!") is True

    def test_real_content_not_noise(self):
        assert is_noise("Mai ơi hôm nay ăn gì chưa") is False
        assert is_noise("con mèo nhà tớ đỡ rồi") is False
        assert is_noise("hello mọi người") is False

    def test_short_but_meaningful_not_over_filtered(self):
        # 3 ký tự có chữ, không nằm trong danh sách phản ứng -> không phải nhiễu
        assert is_noise("lol vl") is False


class TestRoomPulse:
    def test_snapshot_counts(self):
        p = RoomPulse(window_sec=30)
        p.record("u1", "xin chào mọi người", False)
        p.record("u2", "haha", True)
        p.record("u1", "hôm nay ăn gì chưa", False)

        snap = p.snapshot()
        assert snap["msgs_30s"] == 3
        assert snap["active_users_30s"] == 2
        assert 0.0 < snap["noise_ratio"] < 1.0
        assert len(snap["recent_content"]) == 2   # chỉ tin có nội dung

    def test_noise_still_counted_but_not_in_recent_content(self):
        p = RoomPulse(window_sec=30)
        p.record("u1", "haha", True)
        snap = p.snapshot()
        assert snap["msgs_30s"] == 1
        assert snap["recent_content"] == []

    def test_recent_content_capped_at_10(self):
        p = RoomPulse(window_sec=30, keep_content=10)
        for i in range(15):
            p.record("u1", f"tin số {i}", False)
        snap = p.snapshot()
        assert len(snap["recent_content"]) == 10

    def test_window_expires_old_events(self):
        p = RoomPulse(window_sec=0.05)
        p.record("u1", "tin cũ", False)
        time.sleep(0.1)
        snap = p.snapshot()
        assert snap["msgs_30s"] == 0
        assert snap["active_users_30s"] == 0

    def test_empty_snapshot(self):
        p = RoomPulse()
        snap = p.snapshot()
        assert snap["msgs_30s"] == 0
        assert snap["active_users_30s"] == 0
        assert snap["noise_ratio"] == 0.0
        assert snap["recent_content"] == []