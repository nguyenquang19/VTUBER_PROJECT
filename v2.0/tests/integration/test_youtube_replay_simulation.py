from __future__ import annotations

import json
from pathlib import Path

from orchestrator.config_loader import ConfigLoader
from scripts.simulate_youtube_replay import simulate_replay


REPO_ROOT = Path(__file__).resolve().parents[2]


def _chat(message_id: str, text: str, offset_ms: int) -> str:
    return json.dumps({
        "replayChatItemAction": {
            "videoOffsetTimeMsec": str(offset_ms),
            "actions": [{
                "addChatItemAction": {
                    "item": {
                        "liveChatTextMessageRenderer": {
                            "id": message_id,
                            "message": {"simpleText": text},
                            "authorName": {"simpleText": message_id},
                            "authorExternalChannelId": f"channel-{message_id}",
                        },
                    },
                },
            }],
        },
    }, ensure_ascii=False)


async def test_replay_simulator_batches_chat_and_uses_real_director(tmp_path: Path) -> None:
    source = tmp_path / "real-shape.live_chat.json"
    source.write_text("\n".join([
        _chat("normal", "xin chào", 100),
        _chat("question", "hôm nay chơi gì?", 200),
        _chat("mention", "Mai ơi đọc chat em", 300),
    ]) + "\n", encoding="utf-8")
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    report = await simulate_replay(
        source, loader=loader, tick_window_ms=1500, max_trace_items=20,
    )

    assert report["input"]["events"] == 3
    assert report["timing"]["ticks_with_chat"] == 1
    assert report["director"]["turn_kernel"]["turn_kernel_public_owner"] == "COMPATIBILITY"
    assert report["trace"][0]["temporal"]["decision_offset_ms"] == 1500
    assert report["trace"][0]["temporal"]["opportunity_offset_ms"] == 1500
    assert report["trace"][0]["temporal"]["decision_id"]
    assert report["trace"][0]["temporal"]["transaction_id"]
    assert report["trace"][0]["temporal"]["reservation_offset_ms"] == 1500
    assert report["trace"][0]["temporal"]["delivery_offset_ms"] == 1500
    assert report["trace"][0]["temporal"]["commit_offset_ms"] == 1500
    first = report["trace"][0]
    assert first["incoming_count"] == 3
    assert first["action"] in {"read_chat", "continue_thread"}
    assert first["director_v2"]["selection"]["accepted"] is True
    assert first["director_v2"]["selection"]["decision_owner"] == "director_v2"
    assert first["selected"][0]["event_id"] == "mention"
    assert first["selected"][0]["kind"] == "mention"
    assert report["delivery"]["delivered_turns"] == 1
    assert report["delivery"]["generation_attempts"] == 1
    assert report["delivery"]["public_turns"] == 1
    assert report["delivery"]["items"][0]["attempt_id"]
    assert report["delivery"]["items"][0]["turn_id"]
    assert report["delivery"]["transactions"]["committed"] == 1
    assert report["director"]["metrics"]["director_v2_primary_selected_total"] >= 1
    assert report["thought_engine"]["metrics"]["self_talk_planner_enabled"] is True
    threads = report["conversation_threads"]
    assert threads["metrics"]["thread_opened_total"] >= 1
    assert threads["false_commits"] == 0
    assert any(item["threads"] for item in report["trace"])


async def test_replay_simulator_runs_thought_engine_for_dead_air(tmp_path: Path) -> None:
    source = tmp_path / "quiet.live_chat.json"
    # Im lặng đủ dài để vượt dead_air_seconds=28 (config production 1.0.2) rồi mới có chat.
    source.write_text(_chat("late", "Mai ơi", 40_000) + "\n", encoding="utf-8")
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    report = await simulate_replay(
        source, loader=loader, tick_window_ms=1500, max_trace_items=100,
    )

    assert report["director"]["self_talk_cadence"]["count"] >= 1
    assert report["director"]["action_counts"].get("self_talk", 0) <= 1
    assert report["delivery"]["transactions"].get("released", 0) == 0
    metrics = report["thought_engine"]["metrics"]
    assert metrics["self_talk_planner_plans_total"] >= 1
    assert metrics["self_talk_planner_commits_total"] >= 1


async def test_replay_never_delivers_self_talk_on_a_tick_with_new_chat(
    tmp_path: Path,
) -> None:
    source = tmp_path / "chat-priority.live_chat.json"
    source.write_text("\n".join([
        _chat("first", "xin chào", 25_000),
        _chat("second", "mình vẫn ở đây", 50_000),
    ]) + "\n", encoding="utf-8")
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    report = await simulate_replay(
        source, loader=loader, tick_window_ms=1500, max_trace_items=100,
    )

    incoming_ticks = [item for item in report["trace"] if item["incoming_count"] > 0]
    assert incoming_ticks
    assert all(
        not any(
            delivery["request_id"].startswith("self_")
            for delivery in item["deliveries"]
        )
        for item in incoming_ticks
    )


async def test_v2_primary_follow_up_obeys_open_thread_source_cooldown(
    tmp_path: Path,
) -> None:
    source = tmp_path / "thread-cooldown.live_chat.json"
    source.write_text("\n".join([
        _chat("question", "Mai nghĩ sao về cà phê?", 100),
        _chat("late", "mình vẫn nghe đây", 45_000),
    ]) + "\n", encoding="utf-8")
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    report = await simulate_replay(
        source, loader=loader, tick_window_ms=1500, max_trace_items=100,
    )

    assert report["director"]["action_counts"].get("follow_up", 0) <= 1
    assert report["delivery"]["transactions"].get("released", 0) <= 1
