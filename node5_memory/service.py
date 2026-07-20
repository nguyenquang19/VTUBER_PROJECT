import time
from .store import Store
from .schema import UserProfile, ModerationLog, ModStatus
from .moderation import moderate_summary

class Node5Service:
    def __init__(self, store=None):
        self.store = store or Store()

    # 1) Lấy hồ sơ (Node2 gọi trước khi trả lời) — 300ms cứng phía Node2
    def get_profile(self, user_id: str) -> dict | None:
        p = self.store.get_profile(user_id)
        return p.to_dict() if p and p.mod_status == ModStatus.APPROVED else None

    # 2) Ghi tóm tắt sau buổi (job nền) — QUA kiểm duyệt, idempotent theo session
    def write_summary(self, user_id, display_name, session_id, new_summary, source="auto_endsession"):
        # idempotent: cùng session gọi lại KHÔNG tạo bản trùng
        if source == "auto_endsession" and self.store.already_written(user_id, session_id):
            return {"status": "skip_duplicate"}
        old = self.store.get_profile(user_id)
        old_summary = old.summary if old else ""
        ok, reason = moderate_summary(new_summary)
        decision = "approved" if ok else "rejected"
        self.store.log_moderation(ModerationLog(
            user_id, session_id, time.time(), old_summary, new_summary, decision, source))
        if not ok:
            return {"status": "rejected", "reason": reason}   # log nhưng KHÔNG cập nhật
        # cập nhật hồ sơ (chỉ khi duyệt qua)
        p = old or UserProfile(user_id, display_name)
        p.display_name = display_name; p.summary = new_summary
        p.appearances += 1; p.last_mentioned = time.time()
        p.memory_strength = 1.0; p.mod_status = ModStatus.APPROVED
        self.store.upsert_profile(p)
        return {"status": "written"}

    # 3) Tính lại độ mạnh trí nhớ (job định kỳ, không trên đường live)
    def decay(self, half_life_days=30):
        # giảm memory_strength theo thời gian từ last_mentioned
        pass  # v1: khung; điền công thức decay khi cần