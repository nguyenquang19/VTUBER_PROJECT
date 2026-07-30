"""Live LLM test qua /v1/chat/completions (ARCHITECTURE 8.2, 1.B).

Marker llm: cần llama-server chạy (test tự spawn qua process manager với
--reasoning off). Xác nhận: stream ra token content thẳng (reasoning tắt),
TTFT đo được, cancel giữa chừng cắt được stream.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from interfaces.llm import ChatMessage, LLMRequest
from orchestrator.config_loader import ConfigLoader
from services.llm.llama_cpp_llm import LlamaCppLLMService
from services.llm.process_manager import LlamaServerConfig, LlamaServerProcessManager

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
        print(f"OUTPUT: {text[:150]}")
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
