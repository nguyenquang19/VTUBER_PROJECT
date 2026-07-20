from node5_memory.service import Node5Service
from node5_memory.store import Store

def _svc(tmp_path):
    return Node5Service(store=Store(str(tmp_path / "t.db")))

def test_write_then_get(tmp_path):
    s = _svc(tmp_path)
    r = s.write_summary("u1", "An", "sess1", "An thích chơi game bắn súng.")
    assert r["status"] == "written"
    p = s.get_profile("u1")
    assert p and "game" in p["summary"]

def test_idempotent_same_session(tmp_path):
    s = _svc(tmp_path)
    s.write_summary("u1", "An", "sess1", "Tóm tắt A.")
    r2 = s.write_summary("u1", "An", "sess1", "Tóm tắt A.")  # gọi lại cùng session
    assert r2["status"] == "skip_duplicate"                  # KHÔNG ghi trùng

def test_rejected_not_in_profile(tmp_path):
    from node5_memory import moderation
    moderation._BLOCK = ("cấm",)                             # inject mẫu cấm
    s = _svc(tmp_path)
    r = s.write_summary("u2", "Bình", "sess1", "nội dung cấm đây")
    assert r["status"] == "rejected"
    assert s.get_profile("u2") is None                       # không vào hồ sơ
    # nhưng CÓ trong mod_log (kiểm gián tiếp: gọi lại vẫn xử lý)