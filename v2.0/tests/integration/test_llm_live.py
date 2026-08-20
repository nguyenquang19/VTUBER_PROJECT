"""Live LLM test qua /v1/chat/completions (ARCHITECTURE 8.2, 1.B).

Marker llm: cần llama-server chạy (test tự spawn qua process manager với
--reasoning off). Xác nhận: stream ra token content thẳng (reasoning tắt),
TTFT đo được, cancel giữa chừng cắt được stream.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from interfaces.llm import ChatMessage, LLMRequest
from orchestrator.config_loader import ConfigLoader
from services.llm.llama_cpp_llm import LlamaCppLLMService
from services.llm.process_manager import LlamaServerConfig, LlamaServerProcessManager
from services.llm.prompt_cache import PromptCache

REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def loader() -> ConfigLoader:
    ldr = ConfigLoader(REPO_ROOT / "config")
    ldr.load_all()
    return ldr


@pytest.fixture(scope="module")
async def running_server(loader):
    mgr = LlamaServerProcessManager(LlamaServerConfig.from_loader(loader))
    await mgr.start()
    yield mgr
    await mgr.stop()


async def test_chat_streams_content(loader, running_server) -> None:
    svc = LlamaCppLLMService.from_loader(loader)
    await svc.start()
    try:
        request = LLMRequest(
            request_id="live1",
            messages=[
                ChatMessage(role="system", content="Bạn trả lời ngắn gọn bằng tiếng Việt."),
                ChatMessage(role="user", content="Chào bạn, hôm nay trời thế nào? Trả lời 1 câu."),
            ],
            max_tokens=60,
            temperature=0.7,
        )
        tokens = [t async for t in svc.generate_stream(request)]
        text = "".join(t.token for t in tokens)
        assert len(text) > 0, "không sinh được text nào"
        assert any(t.is_final for t in tokens), "thiếu final token"

        m = svc.get_metrics()
        assert m["llm_last_ttft_ms"] is not None
        assert m["llm_last_tokens_out"] > 0
        print(f"\nTTFT={m['llm_last_ttft_ms']:.0f}ms tokens={m['llm_last_tokens_out']} "
              f"decode={m['llm_last_decode_tps']}")
        # PowerShell/CI có thể để stdout ở cp1252; !a giữ debug output ASCII-safe.
        print(f"OUTPUT: {text[:150]!a}")
    finally:
        await svc.stop()


async def test_cancel_midstream(loader, running_server) -> None:
    svc = LlamaCppLLMService.from_loader(loader)
    await svc.start()
    try:
        request = LLMRequest(request_id="live2", prompt="Đếm từ 1 đến 100:", max_tokens=200)
        got = 0
        async for t in svc.generate_stream(request):
            if t.token:
                got += 1
                if got >= 3:
                    await svc.cancel("live2")
        assert got < 200
    finally:
        await svc.stop()


async def test_oversized_correction_is_compacted_before_live_generation(
    loader, running_server,
) -> None:
    svc = LlamaCppLLMService.from_loader(loader)
    await svc.start()
    try:
        prefix = PromptCache.from_loader(loader).as_message()
        latest = (
            "[SELF-THOUGHT] Mỏ neo đã biết: Anami xin đừng ăn tôi. "
            + ("Chỉ bám dữ kiện trong mỏ neo và không tự điền ý người xem. " * 75)
            + "[SỬA HÌNH DÁNG OUTPUT] Giữ nguyên mỏ neo; chỉ viết câu thoại mới."
        )
        request = LLMRequest(
            request_id="live_context_bound",
            messages=[
                prefix,
                ChatMessage(role="user", content="u" * 600),
                ChatMessage(role="assistant", content="a" * 600),
                ChatMessage(role="user", content=latest),
            ],
            max_tokens=8,
            temperature=0.0,
        )
        raw_messages = request.to_messages()
        budget = svc.context_size - request.max_tokens - svc.context_safety_tokens
        started = time.perf_counter()
        measured_counts = [await svc._count_input_tokens(raw_messages) for _ in range(10)]
        preflight_mean_ms = (time.perf_counter() - started) * 1000 / len(measured_counts)
        assert measured_counts[-1] > budget
        print(f"\nTOKEN PREFLIGHT mean={preflight_mean_ms:.3f}ms input={measured_counts[-1]}")

        bounded = await svc._bounded_messages(
            raw_messages, max_tokens=request.max_tokens, request_id=request.request_id,
        )
        assert await svc._count_input_tokens(bounded) <= budget
        assert bounded[0]["content"] == prefix.content
        assert bounded[-1]["content"].endswith(
            "[SỬA HÌNH DÁNG OUTPUT] Giữ nguyên mỏ neo; chỉ viết câu thoại mới."
        )

        tokens = [token async for token in svc.generate_stream(request)]
        assert any(token.is_final for token in tokens)
        metrics = svc.get_metrics()
        assert metrics["llm_context_compactions_total"] >= 2
        assert metrics["llm_context_budget_failures_total"] == 0
        assert metrics["llm_context_counter_failures_total"] == 0
    finally:
        await svc.stop()
