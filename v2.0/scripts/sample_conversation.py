"""Sinh một đoạn hội thoại THẬT từ model hiện tại để nghe/đánh giá sau khi tune.

Chạy kịch bản chat cố định (khen / hỏi / trêu / gọi tên / tâm sự buồn) qua đúng
pipeline production: llama.cpp + prompt_manager + filter + fallback, history multi-turn
tự tích lũy. In ra Người xem → Mai để bạn tự chấm bằng tai/mắt.

Cần llama-server đang chạy (models.yaml::llm_main). Chạy:
    python scripts/sample_conversation.py
    python scripts/sample_conversation.py --out logs/evaluation/sample_convo.txt
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from orchestrator.fallback_manager import FallbackManager  # noqa: E402
from services.filter.regenerator import FilterRegenerator  # noqa: E402
from services.filter.rule_filter import RuleFilter  # noqa: E402
from services.llm.canned_response import CannedResponder  # noqa: E402
from services.llm.llama_cpp_llm import LlamaCppLLMService  # noqa: E402
from services.llm.llm_turn import LLMTurnRunner  # noqa: E402
from services.llm.prompt_manager import PromptManager  # noqa: E402

# Kịch bản phủ các tình huống hay gặp. (câu người xem, category cho mood).
SCRIPT: list[tuple[str, str]] = [
    ("mai ơi hôm nay stream gì thế", "chat_mention_direct"),
    ("trời ơi cậu xinh quá đi mất", "chat_compliment"),
    ("mà cậu chơi game này bao lâu rồi", "chat_question_normal"),
    ("chơi gà thế mà cũng đòi stream à", "chat_insult_troll"),
    ("thôi kệ đi mai, tui vừa chia tay người yêu xong buồn ghê", "chat_genuine_sad_share"),
    ("kể tui nghe chuyện gì vui đi", "chat_question_normal"),
    ("ừ cậu nói tiếp đi", "chat_mention_direct"),
]


async def run(out_path: Path | None) -> int:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()

    service = LlamaCppLLMService.from_loader(loader)
    await service.start()
    health = await service.health_check()
    if not health.is_ok:
        await service.stop()
        print("llama-server chưa healthy — bật server rồi chạy lại.")
        return 1

    filter_service = None
    regenerator = None
    if bool(loader.get("features", "features.filter_rule.enabled", False)):
        filter_service = RuleFilter.from_config(loader)
        regenerator = FilterRegenerator.from_loader(loader, filter_service, service)
        await filter_service.start()

    prompt_manager = PromptManager.from_loader(loader)
    runner = LLMTurnRunner.from_loader(
        loader, service, prompt_manager, FallbackManager(),
        CannedResponder.from_loader(loader),
        regenerator=regenerator, session_id="sample-convo",
    )

    lines: list[str] = []
    inj = loader.get("models", "llm_main.inject_mood_directive", True)
    header = (
        f"=== ĐOẠN HỘI THOẠI MẪU (inject_mood_directive={inj}) ===\n"
        "Đọc: nghe có tự nhiên/đúng chất người không? có nhạt/máy móc/lặp không?\n"
    )
    print(header)
    lines.append(header)
    try:
        for i, (user_text, category) in enumerate(SCRIPT, 1):
            parsed, level = await runner.run_turn(
                request_id=f"sample-{i}", user_text=user_text,
                trigger_type="chat", event_category=category, commit_history=True,
            )
            tag = "" if level == 0 else f"  [fallback L{level}]"
            block = f"Người xem: {user_text}\nMai: {parsed.text}{tag}\n"
            print(block)
            lines.append(block)
    finally:
        if filter_service is not None:
            await filter_service.stop()
        await service.stop()

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"Đã lưu: {out_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Sinh hội thoại mẫu để đánh giá sau tune")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    return asyncio.run(run(args.out))


if __name__ == "__main__":
    raise SystemExit(main())
