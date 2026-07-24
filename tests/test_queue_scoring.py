import time
from node2_core.queue import PriorityInbox
from shared.contracts.payloads import Priority


def _rec(content="", tier1_score=0.1):
    class R:
        pass
    r = R()
    r.content = content
    r.tier1_score = tier1_score
    return r


def test_crisis_always_first_even_enqueued_last():
    q = PriorityInbox()
    q.put(Priority.CHAT, _rec("tin thường"))
    q.put(Priority.AUTO, _rec("tự nói"))
    q.put(Priority.CRISIS, _rec("cứu tôi"))   # vào sau cùng
    pri, rec = q.get()
    assert pri == Priority.CRISIS


def test_chat_waiting_long_beats_new_chat():
    q = PriorityInbox()
    old_rec = _rec("tin cũ chờ lâu")
    q._items.append((Priority.CHAT, next(q._c), old_rec, time.time() - 20))
    q.put(Priority.CHAT, _rec("tin mới toanh"))
    pri, rec = q.get()
    assert rec is old_rec


def test_auto_waiting_long_pushed_below_new_chat():
    q = PriorityInbox()
    old_auto = _rec("tự nói cũ")
    q._items.append((Priority.AUTO, next(q._c), old_auto, time.time() - 20))
    q.put(Priority.CHAT, _rec("tin chat mới"))
    pri, rec = q.get()
    assert pri == Priority.CHAT   # CHAT mới thắng AUTO cũ dù AUTO chờ lâu


def test_expired_item_never_returned():
    q = PriorityInbox()
    expired = _rec("tin quá hạn")
    q._items.append((Priority.CHAT, next(q._c), expired, time.time() - 999))
    got = q.get()
    assert got is None


def test_mention_mai_and_high_tier1_boost_score():
    q = PriorityInbox()
    plain = _rec("chuyện phiếm", tier1_score=0.1)
    urgent = _rec("Mai ơi giúp mình với", tier1_score=0.5)
    q.put(Priority.CHAT, plain)
    q.put(Priority.CHAT, urgent)
    pri, rec = q.get()
    assert rec is urgent


def test_fifo_tiebreak_when_score_equal():
    q = PriorityInbox()
    first = _rec("a")
    second = _rec("a")
    q.put(Priority.CHAT, first)
    q.put(Priority.CHAT, second)
    pri, rec = q.get()
    assert rec is first


def test_get_size_and_has_pending_crisis():
    q = PriorityInbox()
    assert q.get_size() == 0
    assert q.has_pending_crisis() is False
    q.put(Priority.CRISIS, _rec("khẩn"))
    assert q.get_size() == 1
    assert q.has_pending_crisis() is True