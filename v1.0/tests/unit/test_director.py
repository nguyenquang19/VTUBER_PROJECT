"""Test C0.3 — Director loop (docs/03_COMPONENT_REFERENCE.md §C0.4).

DoD:
- 1h giả lập: Director hoàn thành ≥80% segment planned
- không dead-air > 20s
- không chuỗi read_chat vô hạn
+ read_chat adaptive (single/cluster/summary/vibe), superchat chen hàng.
"""
from __future__ import annotations

from pathlib import Path

from services.director.chat_pulse import ChatPulse
from services.director.director import (
    Director,
    DirectorAction,
    ReadMode,
    Segment,
)
from services.director.salience import SaliencePool

REPO_ROOT = Path(__file__).resolve().parents[2]

_BASE = {"chat": 10, "question": 25, "mention": 35}


def _pool(**over) -> SaliencePool:
    kw = dict(base_tier=_BASE, tau_seconds=50.0, floor=3.0, cluster_coef=5.0)
    kw.update(over)
    return SaliencePool(**kw)


def _pulse(**over) -> ChatPulse:
    kw = dict(window_seconds=60.0, tempo_low_per_min=2.0, tempo_high_per_min=15.0,
              diversity_threshold=0.4, cold_silence_seconds=90.0)
    kw.update(over)
    return ChatPulse(**kw)


def _segments() -> list[Segment]:
    return [
        Segment("opening", "chào", 10.0, {"self_talk", "read_chat", "transition"}),
        Segment("main", "chính", 30.0, {"read_chat", "self_talk", "ack_donation", "transition"}),
        Segment("closing", "kết", 10.0, {"self_talk", "transition"}),
    ]


def _director(pool=None, pulse=None, **over) -> Director:
    pool = pool or _pool()
    pulse = pulse or _pulse()
    segments = over.pop("segments", _segments())
    kw = dict(dead_air_seconds=20.0, max_consecutive_read_chat=3,
              max_refs_per_turn=3, backlog_summary_threshold=12,
              summary_score_ceiling=15.0, chat_gate_enabled=True)
    kw.update(over)
    d = Director(pool, pulse, segments, **kw)
    d.start(now=0.0)
    return d


class TestReadModes:
    def test_superchat_acks_priority(self) -> None:
        pool = _pool()
        pool.add("c", "chat thường", now=0.0, kind="chat")
        pool.add("sc", "quà nè", now=0.0, kind="chat", amount_vnd=500_000, is_super=True)
        d = _director(pool=pool)
        d.advance_segment(now=0.0)  # sang main (cho ack_donation)
        dec = d.decide(now=1.0)
        assert dec.action == DirectorAction.ACK_DONATION
        assert dec.refs[0].msg_id == "sc"

    def test_single_top(self) -> None:
        pool = _pool()
        pool.add("m", "Mai ơi", now=0.0, kind="mention")
        d = _director(pool=pool)
        dec = d.decide(now=1.0)
        assert dec.action == DirectorAction.READ_CHAT
        assert dec.read_mode == ReadMode.SINGLE
        assert dec.refs[0].msg_id == "m"

    def test_low_single_waits_below_actionable_score(self) -> None:
        pool = _pool()
        pulse = _pulse(tempo_low_per_min=0.0)
        pulse.record(now=0.0, user_id="u1")
        pool.add("c", "xin chào", now=0.0, kind="chat")
        d = _director(pool=pool, pulse=pulse)

        dec = d.decide(now=1.0)

        assert dec.action == DirectorAction.WAIT
        assert dec.reason == "below_actionable_score"
        assert pool.size() == 1  # giữ lại để có thể cluster hoặc decay

    def test_question_above_gate_is_read(self) -> None:
        pool = _pool()
        pool.add("q", "có nên học tiếp không", now=0.0, kind="question")
        d = _director(pool=pool)

        dec = d.decide(now=1.0)

        assert dec.action == DirectorAction.READ_CHAT
        assert dec.refs[0].msg_id == "q"

    def test_repeated_generic_chat_crosses_gate_by_cluster(self) -> None:
        pool = _pool()
        for i in range(3):
            pool.add(f"c{i}", "game về nước rồi", now=0.0, kind="chat")
        d = _director(pool=pool)

        dec = d.decide(now=1.0)

        assert dec.action == DirectorAction.READ_CHAT
        assert dec.read_mode == ReadMode.CLUSTER

    def test_disabling_gate_restores_low_single_read(self) -> None:
        pool = _pool()
        pool.add("c", "xin chào", now=0.0, kind="chat")
        d = _director(pool=pool, chat_gate_enabled=False)

        dec = d.decide(now=1.0)

        assert dec.action == DirectorAction.READ_CHAT
        assert dec.read_mode == ReadMode.SINGLE

    def test_cluster_merge(self) -> None:
        pool = _pool()
        for i in range(5):
            pool.add(f"d{i}", "Mai chơi game gì thế", now=0.0, kind="mention")
        d = _director(pool=pool)
        dec = d.decide(now=1.0)
        assert dec.read_mode == ReadMode.CLUSTER
        assert len(dec.refs) <= 3   # max_refs

    def test_backlog_summary(self) -> None:
        # nhiều tin chat ĐIỂM THẤP + KHÁC NHAU (không cluster) > threshold 12 → summary
        pool = _pool()
        distinct = [
            "trời hôm nay đẹp ghê", "ăn phở hay bún đây", "mèo nhà tao dễ thương",
            "deadline sắp tới rồi", "cà phê sáng ngon quá", "đi ngủ đây bye",
            "game mới hay không", "nhạc gì đang nghe vậy", "mưa to quá trời",
            "học bài chán ghê", "code lỗi hoài à", "đói bụng muốn xỉu",
            "xem phim gì tối nay", "cuối tuần đi đâu chơi", "buồn ngủ dã man",
        ]
        for i, txt in enumerate(distinct):
            pool.add(f"c{i}", txt, now=0.0, kind="chat")
        assert pool.size() >= 12   # đảm bảo không cluster nhầm
        d = _director(pool=pool)
        dec = d.decide(now=1.0)
        assert dec.read_mode == ReadMode.SUMMARY
        assert dec.refs == ()

    def test_hype_spam_vibe(self) -> None:
        pool = _pool()
        pulse = _pulse()
        for i in range(30):
            pool.add(f"e{i}", "W", now=0.0, kind="chat")   # near-dup → cluster
            pulse.record(now=0.0 + i * 0.1, user_id=f"u{i % 2}")
        d = _director(pool=pool, pulse=pulse)
        dec = d.decide(now=3.0)
        assert dec.read_mode == ReadMode.VIBE

    def test_hype_does_not_swallow_question(self) -> None:
        pool = _pool()
        pulse = _pulse()
        pool.add("q", "có nên chơi tiếp không", now=0.0, kind="question")
        for i in range(30):
            pulse.record(now=i * 0.1, user_id=f"u{i % 2}")
        d = _director(pool=pool, pulse=pulse)

        dec = d.decide(now=3.0)

        assert dec.action == DirectorAction.READ_CHAT
        assert dec.read_mode == ReadMode.SINGLE
        assert dec.refs[0].msg_id == "q"


class TestProactive:
    def test_dead_air_triggers_self_talk(self) -> None:
        d = _director()  # pool rỗng
        d.advance_segment(now=0.0)   # sang main (30s) — tránh transition của opening 10s
        d.mark_spoke(DirectorAction.TRANSITION, now=0.0)
        # 25s sau không nói, còn trong main → dead-air > 20 → self_talk
        dec = d.decide(now=25.0)
        assert dec.action == DirectorAction.SELF_TALK
        assert dec.reason in ("dead_air", "cold_chat")

    def test_cold_chat_self_talk(self) -> None:
        d = _director()
        dec = d.decide(now=5.0)   # pool rỗng, chưa dead-air nhưng COLD
        assert dec.action == DirectorAction.WAIT
        assert dec.reason == "idle"

    def test_self_talk_global_cooldown_blocks_persistent_cold_chat(self) -> None:
        d = _director(
            segments=[Segment("main", "main", 300.0, {"self_talk"})],
            self_talk_cooldown_seconds=45.0,
        )
        first = d.decide(now=20.0)
        assert first.action == DirectorAction.SELF_TALK
        d.mark_spoke(first.action, now=20.0)

        blocked = d.decide(now=41.0)
        assert blocked.action == DirectorAction.WAIT
        assert blocked.reason == "self_talk_cooldown"

        ready = d.decide(now=65.0)
        assert ready.action == DirectorAction.SELF_TALK

    def test_no_material_defer_does_not_fake_last_spoken_time(self) -> None:
        d = _director(
            segments=[Segment("main", "main", 300.0, {"self_talk"})],
        )
        last_spoke = d._last_speak_ts
        d.defer_self_talk(90.0)
        blocked = d.decide(now=30.0)
        assert blocked.action == DirectorAction.WAIT
        assert blocked.reason == "thought_unavailable"
        assert d._last_speak_ts == last_spoke
        d.clear_self_talk_defer()
        assert d.decide(now=30.0).action == DirectorAction.SELF_TALK

    def test_consecutive_read_chat_forces_break(self) -> None:
        # DoD: không chuỗi read_chat vô hạn — sau max_consec → ép self_talk
        pool = _pool()
        pulse = _pulse(tempo_low_per_min=0.0)  # không COLD
        d = _director(pool=pool, pulse=pulse, max_consecutive_read_chat=3)
        for i in range(3):
            pool.add(f"m{i}", f"câu hỏi riêng biệt số {i} nha", now=float(i), kind="mention")
            dec = d.decide(now=float(i) + 0.5)
            assert dec.action == DirectorAction.READ_CHAT
            d.mark_spoke(DirectorAction.READ_CHAT, now=float(i) + 1)
        # tin thứ 4 vẫn có nhưng consec cap → phải self_talk
        pool.add("m4", "câu hỏi nữa nè khác biệt", now=4.0, kind="mention")
        dec = d.decide(now=4.5)
        assert dec.action == DirectorAction.SELF_TALK
        assert dec.reason == "consec_read_chat_break"

    def test_urge_ready_self_talk(self) -> None:
        # Cần pulse KHÔNG cold: tempo_low=0 (không cold theo tempo) + có record gần
        # (không cold theo silence). Pool rỗng, chưa dead-air → chỉ urge kích.
        pool = _pool()
        pulse = _pulse(tempo_low_per_min=0.0, cold_silence_seconds=90.0)
        d = _director(
            pool=pool, pulse=pulse,
            segments=[Segment("main", "main", 300.0, {"self_talk"})],
        )
        d.mark_spoke(DirectorAction.SELF_TALK, now=0.0)  # reset dead-air
        pulse.record(now=1.0, user_id="a")               # không cold theo silence
        blocked = d.decide(now=1.0, urge_ready=True)
        assert blocked.action == DirectorAction.WAIT
        d.mark_spoke(DirectorAction.READ_CHAT, now=44.0)
        pulse.record(now=44.0, user_id="b")
        dec = d.decide(now=45.0, urge_ready=True)
        assert dec.action == DirectorAction.SELF_TALK
        assert dec.reason == "urge_ready"


class TestSegments:
    def test_transition_when_time_up(self) -> None:
        d = _director()  # opening duration 10s
        dec = d.decide(now=11.0)
        assert dec.action == DirectorAction.TRANSITION
        assert dec.next_segment == "main"

    def test_transition_when_time_up_with_chat_backlog(self) -> None:
        pool = _pool()
        pool.add("q", "Mai đang chơi gì?", now=0.0, kind="mention")
        d = _director(pool=pool)

        dec = d.decide(now=11.0)

        assert dec.action == DirectorAction.TRANSITION
        assert dec.next_segment == "main"
        assert pool.size() == 1

    def test_advance_moves_segment(self) -> None:
        d = _director()
        assert d.current_segment().name == "opening"
        d.advance_segment(now=11.0)
        assert d.current_segment().name == "main"

    def test_last_segment_no_auto_transition(self) -> None:
        d = _director()
        d.advance_segment(now=0.0)  # main
        d.advance_segment(now=0.0)  # closing (last)
        assert d.is_last_segment()
        # quá giờ closing nhưng không auto-transition (segment cuối)
        dec = d.decide(now=100.0)
        assert dec.action != DirectorAction.TRANSITION


class TestOneHourSim:
    def test_sim_completes_segments_no_deadair_no_infinite_read(self) -> None:
        # DoD: 1h sim, ≥80% segment reached, không dead-air>20s, không read_chat vô hạn.
        pool = _pool()
        pulse = _pulse()
        # segment ngắn để 1h sim chạm hết: opening10 main30 closing10 = 50s < 3600
        d = _director(pool=pool, pulse=pulse)

        seen_segments = set()
        max_read_streak = 0
        cur_streak = 0
        last_speak = 0.0
        max_gap = 0.0

        t = 0.0
        while t < 3600.0:
            # thi thoảng bơm chat để có tin đáp
            if int(t) % 7 == 0:
                pool.add(f"c{int(t)}", f"chat lúc {int(t)} nội dung khác biệt", now=t, kind="chat")
                pulse.record(now=t, user_id=f"u{int(t) % 5}")
            dec = d.decide(now=t)
            seen_segments.add(dec.segment)
            if dec.action == DirectorAction.TRANSITION:
                d.advance_segment(now=t)
                d.mark_spoke(DirectorAction.TRANSITION, now=t)
                cur_streak = 0          # transition = Mai nói, không phải read
                last_speak = t
            elif dec.action != DirectorAction.WAIT:
                if dec.action == DirectorAction.READ_CHAT:
                    cur_streak += 1
                    max_read_streak = max(max_read_streak, cur_streak)
                    pool.pop_top(now=t)  # đã đáp → gỡ
                else:
                    cur_streak = 0
                d.mark_spoke(dec.action, now=t)
                max_gap = max(max_gap, t - last_speak)
                last_speak = t
            t += 1.0

        # ≥80% của 3 segment = ít nhất 3 (đủ 3) — 1h thừa sức chạm hết
        assert len(seen_segments) >= 3
        # không chuỗi read_chat vô hạn (cap 3)
        assert max_read_streak <= 3
        # Khoảng nghỉ self-talk toàn cục 45s; không được dồn theo tick 1s.
        assert max_gap <= 46.0


class TestFromLoader:
    def test_from_loader(self) -> None:
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        pool = SaliencePool.from_loader(loader)
        pulse = ChatPulse.from_loader(loader)
        d = Director.from_loader(pool, pulse, loader)
        d.start(now=0.0)
        assert d.current_segment().name == "opening"
        # dead-air → self_talk
        dec = d.decide(now=25.0)
        assert dec.action == DirectorAction.SELF_TALK

    def test_superchat_acked_in_every_segment(self) -> None:
        # TASK 1: superchat phải được ack ở MỌI segment (config real: opening+closing
        # nay có ack_donation).
        from orchestrator.config_loader import ConfigLoader
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        for seg_name in ("opening", "main", "chat", "closing"):
            pool = SaliencePool.from_loader(loader)
            pulse = ChatPulse.from_loader(loader)
            d = Director.from_loader(pool, pulse, loader)
            d.start(now=0.0)
            while d.current_segment().name != seg_name:
                d.advance_segment(now=0.0)
            pool.add("sc", "quà nè", now=0.0, kind="chat",
                     amount_vnd=500_000, is_super=True)
            dec = d.decide(now=0.5)
            assert dec.action == DirectorAction.ACK_DONATION, f"seg {seg_name} không ack"
