import time
from node1_ingestion.rate_limiter import RateLimiter


def test_under_limit_always_allowed():
    rl = RateLimiter(window_s=0, room_window_s=10, room_limit=3)
    for _ in range(3):
        assert rl.allow_room("xin chào", 0.1) is True


def test_over_limit_blocks_low_score_no_mention():
    rl = RateLimiter(window_s=0, room_window_s=10, room_limit=2)
    rl.allow_room("a", 0.1)
    rl.allow_room("b", 0.1)
    assert rl.allow_room("c", 0.1) is False


def test_over_limit_allows_mention_mai():
    rl = RateLimiter(window_s=0, room_window_s=10, room_limit=2)
    rl.allow_room("a", 0.1)
    rl.allow_room("b", 0.1)
    assert rl.allow_room("Mai ơi trả lời đi", 0.1) is True


def test_over_limit_allows_high_score():
    rl = RateLimiter(window_s=0, room_window_s=10, room_limit=2)
    rl.allow_room("a", 0.1)
    rl.allow_room("b", 0.1)
    assert rl.allow_room("nội dung thường", 0.99) is True


def test_window_expires():
    rl = RateLimiter(window_s=0, room_window_s=0.05, room_limit=1)
    rl.allow_room("a", 0.1)
    rl.allow_room("b", 0.1)          # đã vượt ngưỡng trong cửa sổ cũ
    time.sleep(0.1)
    assert rl.allow_room("c", 0.1) is True   # cửa sổ mới, tính lại từ đầu