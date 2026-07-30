"""Test LlamaCppLLMService streaming/cancel/metrics (ARCHITECTURE 8.2, 1.B).

Stream giờ đi qua raw asyncio socket (KHÔNG httpx) — mock bằng cách inject
`asyncio.StreamReader` đã nạp sẵn bytes HTTP+SSE vào `_open_connection`.
Health vẫn dùng httpx → test riêng bằng httpx.MockTransport.
Live test ở tests/integration (marker llm)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from interfaces.llm import ChatMessage, LLMRequest
from services.llm.llama_cpp_llm import LlamaCppError, LlamaCppLLMService

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------- helpers dựng response HTTP+SSE giả ----------

def chat_stream(tokens: list[str], finish: str = "stop") -> list[dict]:
    """Dựng list chunk kiểu /v1/chat/completions (delta.content + chunk cuối finish)."""
    chunks = [{"choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}]}
              for t in tokens]
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    return chunks


def sse_body(chunks: list[dict], done: bool = True) -> bytes:
    out = b""
    for c in chunks:
        out += b"data: " + json.dumps(c).encode("utf-8") + b"\n\n"
    if done:
        out += b"data: [DONE]\n\n"
    return out


def http_response(body: bytes, status: bytes = b"HTTP/1.1 200 OK") -> bytes:
    return status + b"\r\nContent-Type: text/event-stream\r\n\r\n" + body


class FakeWriter:
    def __init__(self) -> None:
        self.buf = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buf += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


def make_service(response_bytes: bytes) -> tuple[LlamaCppLLMService, FakeWriter]:
    """Service với _open_connection trả reader đã nạp sẵn response_bytes."""
    svc = LlamaCppLLMService(base_url="http://test:8080")
    reader = asyncio.StreamReader()
    reader.feed_data(response_bytes)
    reader.feed_eof()
    writer = FakeWriter()

    async def _opener():
        return reader, writer

    svc._open_connection = _opener  # type: ignore[assignment]
    return svc, writer


def make_http_service(handler) -> LlamaCppLLMService:
    """Service dùng httpx MockTransport — CHỈ cho health check."""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return LlamaCppLLMService(base_url="http://test", client=client)


def req(prompt: str = "xin chào", **kw) -> LLMRequest:
    return LLMRequest(request_id="r1", prompt=prompt, **kw)


def sent_payload(writer: FakeWriter) -> dict:
    _, _, body = writer.buf.partition(b"\r\n\r\n")
    return json.loads(body)


class TestStreaming:
    async def test_yields_tokens_in_order(self) -> None:
        svc, _ = make_service(http_response(sse_body(chat_stream(["Chào", " ", "cậu"]))))
        tokens = [t async for t in svc.generate_stream(req())]
        content = [t.token for t in tokens if t.token]
        assert content == ["Chào", " ", "cậu"]

    async def test_final_token_flagged(self) -> None:
        svc, _ = make_service(http_response(sse_body(chat_stream(["a", "b"]))))
        tokens = [t async for t in svc.generate_stream(req())]
        assert tokens[-1].is_final is True
        assert tokens[-1].metadata["finish_reason"] == "stop"
        assert tokens[-1].metadata["tokens_predicted"] == 2
        assert sum(1 for t in tokens if t.is_final) == 1

    async def test_request_id_propagated(self) -> None:
        svc, _ = make_service(http_response(sse_body(chat_stream(["x"]))))
        tokens = [t async for t in svc.generate_stream(req())]
        assert all(t.request_id == "r1" for t in tokens)

    async def test_payload_uses_chat_endpoint_and_cache_prompt(self) -> None:
        svc, writer = make_service(http_response(sse_body(chat_stream(["ok"]))))
        _ = [t async for t in svc.generate_stream(req(max_tokens=123, temperature=0.7))]
        assert writer.buf.startswith(b"POST /v1/chat/completions ")  # KHÔNG /completion
        body = sent_payload(writer)
        assert body["cache_prompt"] is True
        assert body["stream"] is True
        assert body["max_tokens"] == 123
        assert body["temperature"] == 0.7
        # prompt đơn được bọc thành 1 user message
        assert body["messages"] == [{"role": "user", "content": "xin chào"}]

    async def test_messages_field_takes_priority(self) -> None:
        request = LLMRequest(
            request_id="r1",
            messages=[
                ChatMessage(role="system", content="Bạn là Mai."),
                ChatMessage(role="user", content="chào"),
            ],
        )
        svc, writer = make_service(http_response(sse_body(chat_stream(["hi"]))))
        _ = [t async for t in svc.generate_stream(request)]
        body = sent_payload(writer)
        assert body["messages"] == [
            {"role": "system", "content": "Bạn là Mai."},
            {"role": "user", "content": "chào"},
        ]

    async def test_ignores_non_data_and_chunksize_lines(self) -> None:
        # thêm dòng keepalive + dòng kiểu chunk-size hex (không có "data:")
        body = b": keepalive\n\n" + b"1a\r\n" + sse_body(chat_stream(["hi"]))
        svc, _ = make_service(http_response(body))
        tokens = [t.token for t in [x async for x in svc.generate_stream(req())] if t.token]
        assert tokens == ["hi"]

    async def test_malformed_json_skipped(self) -> None:
        body = b"data: {not json}\n\n" + sse_body(chat_stream(["ok"]))
        svc, _ = make_service(http_response(body))
        tokens = [t.token for t in [x async for x in svc.generate_stream(req())] if t.token]
        assert tokens == ["ok"]

    async def test_reasoning_content_fallback(self) -> None:
        # phòng khi server rò reasoning_content thay vì content
        chunks = [{"choices": [{"delta": {"reasoning_content": "ừ"}, "finish_reason": None}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        svc, _ = make_service(http_response(sse_body(chunks)))
        tokens = [t.token for t in [x async for x in svc.generate_stream(req())] if t.token]
        assert tokens == ["ừ"]


class TestErrors:
    async def test_http_error_raises(self) -> None:
        svc, _ = make_service(http_response(b"boom", status=b"HTTP/1.1 500 Internal Server Error"))
        with pytest.raises(LlamaCppError, match="HTTP 500"):
            _ = [t async for t in svc.generate_stream(req())]
        assert svc.get_metrics()["llm_errors_total"] == 1

    async def test_connection_closed_early_raises(self) -> None:
        svc, _ = make_service(b"")  # EOF ngay, không status line
        with pytest.raises(LlamaCppError):
            _ = [t async for t in svc.generate_stream(req())]


class TestCancel:
    async def test_cancel_stops_stream(self) -> None:
        big = chat_stream([str(i) for i in range(100)])
        svc, writer = make_service(http_response(sse_body(big)))
        got = []
        async for t in svc.generate_stream(req()):
            if t.token:
                got.append(t.token)
                await svc.cancel("r1")  # cancel ngay sau token đầu
        assert len(got) < 100
        assert writer.closed is True  # socket được đóng ở finally


class TestMetrics:
    async def test_ttft_and_tokens_recorded(self) -> None:
        svc, _ = make_service(http_response(sse_body(chat_stream(["a", "b", "c"]))))
        _ = [t async for t in svc.generate_stream(req())]
        m = svc.get_metrics()
        assert m["llm_requests_total"] == 1
        assert m["llm_last_ttft_ms"] is not None
        assert m["llm_last_tokens_out"] == 3

    async def test_requests_counter_increments(self) -> None:
        svc, _ = make_service(http_response(sse_body(chat_stream(["x"]))))
        _ = [t async for t in svc.generate_stream(req())]
        # nạp lại reader cho lần 2 (reader cũ đã EOF)
        reader2 = asyncio.StreamReader()
        reader2.feed_data(http_response(sse_body(chat_stream(["y"]))))
        reader2.feed_eof()

        async def _opener2():
            return reader2, FakeWriter()

        svc._open_connection = _opener2  # type: ignore[assignment]
        _ = [t async for t in svc.generate_stream(req())]
        assert svc.get_metrics()["llm_requests_total"] == 2


class TestHealth:
    async def test_health_healthy(self) -> None:
        def handler(request):
            assert request.url.path == "/health"
            return httpx.Response(200, json={"status": "ok"})

        svc = make_http_service(handler)
        h = await svc.health_check()
        assert h.is_ok is True

    async def test_health_unreachable(self) -> None:
        def handler(request):
            raise httpx.ConnectError("refused")

        svc = make_http_service(handler)
        h = await svc.health_check()
        assert h.is_ok is False


class TestFromLoader:
    def test_reads_config(self) -> None:
        from orchestrator.config_loader import ConfigLoader

        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        svc = LlamaCppLLMService.from_loader(loader)
        assert svc.base_url == "http://127.0.0.1:8080"
        assert svc.default_max_tokens == 300
        assert svc._host == "127.0.0.1"
        assert svc._port == 8080
