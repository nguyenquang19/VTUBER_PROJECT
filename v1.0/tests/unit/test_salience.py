"""Test C0.1 — SaliencePool (ROADMAP §C0.1).

DoD:
- superchat 500k luôn được nhặt trước chat thường
- tin > 2*tau không bao giờ thành turn (decay dưới floor)
- 20 tin trùng → 1 turn gộp (cluster)
"""
from __future__ import annotations

from pathlib import Path

from services.director.salience import SaliencePool

REPO_ROOT = Path(__file__).resolve().parents[2]

_BASE = {"chat": 10, "question": 25, "mention": 35}


def _pool(**over) -> SaliencePool:
    kw = dict(
        base_tier=_BASE, superchat_coef=40.0, superchat_divisor=1000.0,
        tau_seconds=50.0, dedup_threshold=0.6, cluster_coef=5.0,
        pool_max=50, floor=3.0,
    )
    kw.update(over)
    return SaliencePool(**kw)


class TestScoring:
    def test_mention_beats_question_beats_chat(self) -> None:
        p = _pool()
        p.add("c", "chào Mai", now=0.0, kind="chat")
        p.add("q", "sao thế Mai", now=0.0, kind="question")
        p.add("m", "Mai ơi", now=0.0, kind="mention")
        top = p.peek_top(now=0.0)
        assert top.msg_id == "m"

    def test_superchat_500k_beats_normal_chat(self) -> None:
        # DoD: superchat 500k luôn nhặt trước chat thường
        p = _pool()
        p.add("chat", "chat thường thôi", now=0.0, kind="chat")
        p.add("sc", "cảm ơn Mai", now=0.0, kind="chat",
              amount_vnd=500_000, is_super=True)
        top = p.peek_top(now=0.0)
        assert top.msg_id == "sc"

    def test_superchat_500k_beats_even_mention(self) -> None:
        p = _pool()
        p.add("m", "Mai ơi Mai", now=0.0, kind="mention")
        p.add("sc", "quà nè", now=0.0, kind="chat",
              amount_vnd=500_000, is_super=True)
        assert p.peek_top(now=0.0).msg_id == "sc"


class TestDecay:
    def test_old_message_decays_below_floor(self) -> None:
        # DoD: tin > 2*tau không surface (chat=10, sau 2τ ~ 10*e^-2=1.35 < floor 3)
        p = _pool()
        p.add("old", "chào", now=0.0, kind="chat")
        # 2*tau = 100s
        assert p.peek_top(now=120.0) is None

    def test_fresh_beats_old_same_tier(self) -> None:
        p = _pool()
        p.add("old", "câu cũ", now=0.0, kind="mention")
        p.add("new", "câu mới", now=90.0, kind="mention")
        assert p.peek_top(now=90.0).msg_id == "new"

    def test_evict_stale_removes_decayed(self) -> None:
        p = _pool()
        p.add("old", "chào", now=0.0, kind="chat")
        p.add("fresh", "Mai ơi", now=100.0, kind="mention")
        removed = p.evict_stale(now=100.0)
        assert removed == 1
        assert p.size() == 1


class TestCluster:
    def test_20_near_duplicates_become_one(self) -> None:
        # DoD: 20 tin trùng → 1 đại diện, cluster_count=20
        p = _pool()
        for i in range(20):
            p.add(f"m{i}", "Mai chơi gì thế Mai", now=0.0, kind="mention")
        assert p.size() == 1
        top = p.peek_top(now=0.0)
        assert top.cluster_count == 20

    def test_cluster_raises_score(self) -> None:
        p = _pool()
        p.add("solo", "câu hỏi riêng lẻ hoàn toàn khác biệt nha", now=0.0, kind="question")
        for i in range(10):
            p.add(f"d{i}", "mọi người ơi cùng hỏi cái này", now=0.0, kind="chat")
        # cụm 10 chat (base 10 + cluster bonus) so với question base 25
        top = p.peek_top(now=0.0)
        # cluster bonus = 5*log1p(9) ~ 11.5 → 10+11.5=21.5 < 25, question vẫn thắng
        # nhưng cụm phải trên floor và có mặt
        assert top.msg_id == "solo"
        clustered = [m for m in p.top_cluster(now=0.0, max_refs=5) if m.msg_id == "d0"]
        assert clustered and clustered[0].cluster_count == 10

    def test_distinct_messages_not_clustered(self) -> None:
        p = _pool()
        p.add("a", "hôm nay trời đẹp quá", now=0.0)
        p.add("b", "ăn cơm chưa mọi người", now=0.0)
        assert p.size() == 2

    def test_superchat_upgrades_cluster_rep(self) -> None:
        # đại diện thường, sau đó 1 bản trùng là superchat → nâng base
        p = _pool()
        p.add("first", "cảm ơn Mai nhiều nha", now=0.0, kind="chat")
        p.add("sc", "cảm ơn Mai nhiều nha", now=0.0, kind="chat",
              amount_vnd=500_000, is_super=True)
        assert p.size() == 1
        top = p.peek_top(now=0.0)
        assert top.is_super and top.cluster_count == 2


class TestPoolCap:
    def test_cap_evicts_lowest(self) -> None:
        p = _pool(pool_max=3)
        p.add("m", "Mai ơi", now=0.0, kind="mention")          # cao
        p.add("q", "sao thế nhỉ bạn", now=0.0, kind="question")
        p.add("c1", "chat một hai ba", now=0.0, kind="chat")   # thấp
        p.add("c2", "chat bốn năm sáu", now=0.0, kind="chat")  # thêm → evict thấp nhất
        assert p.size() == 3
        # mention phải còn (điểm cao nhất)
        ids = {m.msg_id for m in p.top_cluster(now=0.0, max_refs=10)}
        assert "m" in ids


class TestPopAndFromLoader:
    def test_pop_removes(self) -> None:
        p = _pool()
        p.add("m", "Mai ơi", now=0.0, kind="mention")
        popped = p.pop_top(now=0.0)
        assert popped.msg_id == "m"
        assert p.size() == 0

    def test_from_loader(self) -> None:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        p = SaliencePool.from_loader(loader)
        p.add("sc", "quà", now=0.0, kind="chat", amount_vnd=500_000, is_super=True)
        p.add("c", "chat thường", now=0.0, kind="chat")
        assert p.peek_top(now=0.0).msg_id == "sc"
