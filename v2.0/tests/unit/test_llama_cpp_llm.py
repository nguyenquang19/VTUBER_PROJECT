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

    async def _counter(_messages):
        return 1

    svc._open_connection = _opener  # type: ignore[assignment]
    svc._count_input_tokens = _counter  # type: ignore[assignment]
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

    async def test_sampling_params_in_payload(self) -> None:
        # de-AI register: min_p/repeat_penalty/presence_penalty vào payload
        svc, writer = make_service(http_response(sse_body(chat_stream(["ok"]))))
        svc._sampling = {"min_p": 0.05, "repeat_penalty": 1.08, "presence_penalty": 0.3}
        _ = [t async for t in svc.generate_stream(req())]
        body = sent_payload(writer)
        assert body["min_p"] == 0.05
        assert body["repeat_penalty"] == 1.08
        assert body["presence_penalty"] == 0.3

    async def test_sampling_none_not_sent(self) -> None:
        # key None → KHÔNG gửi (không đè default llama-server)
        svc, writer = make_service(http_response(sse_body(chat_stream(["ok"]))))
        # constructor lọc None
        svc2 = LlamaCppLLMService(base_url="http://t:8080",
                                  sampling={"min_p": 0.05, "top_k": None})
        assert "top_k" not in svc2._sampling and svc2._sampling["min_p"] == 0.05

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


    async def test_optional_seed_is_forwarded_for_deterministic_replay(self) -> None:
        svc, writer = make_service(http_response(sse_body(chat_stream(["ok"]))))
        _ = [token async for token in svc.generate_stream(req(seed=20260809))]
        assert sent_payload(writer)["seed"] == 20260809


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


class TestBoundedContext:
    @staticmethod
    def _char_counter(service: LlamaCppLLMService) -> None:
        async def _counter(messages):
            return sum(len(message["content"]) for message in messages)

        service._count_input_tokens = _counter  # type: ignore[assignment]

    async def test_request_under_budget_is_copied_without_content_change(self) -> None:
        service = LlamaCppLLMService(
            context_size=100, context_safety_tokens=10,
            context_min_aux_chars=5, context_min_latest_chars=10,
        )
        self._char_counter(service)
        messages = [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "latest"},
        ]
        bounded = await service._bounded_messages(
            messages, max_tokens=20, request_id="under",
        )
        assert bounded == messages
        assert bounded is not messages
        assert service.get_metrics()["llm_context_compactions_total"] == 0

    async def test_overflow_drops_oldest_complete_history_pair_first(self) -> None:
        service = LlamaCppLLMService(
            context_size=100, context_safety_tokens=10,
            context_min_aux_chars=5, context_min_latest_chars=10,
        )
        self._char_counter(service)
        messages = [
            {"role": "system", "content": "p" * 20},
            {"role": "user", "content": "u" * 30},
            {"role": "assistant", "content": "a" * 30},
            {"role": "user", "content": "z" * 40},
        ]
        bounded = await service._bounded_messages(
            messages, max_tokens=20, request_id="history",
        )
        assert bounded == [messages[0], messages[-1]]
        assert messages[1]["content"] == "u" * 30
        metrics = service.get_metrics()
        assert metrics["llm_context_compactions_total"] == 1
        assert metrics["llm_context_dropped_messages_total"] == 2
        assert metrics["llm_context_last_input_tokens"] == 60

    async def test_overflow_compacts_middle_of_latest_and_preserves_both_ends(self) -> None:
        service = LlamaCppLLMService(
            context_size=100, context_safety_tokens=10,
            context_min_aux_chars=5, context_min_latest_chars=10,
        )
        self._char_counter(service)
        original = "ANCHOR-" + ("x" * 80) + "-CORRECTION"
        messages = [
            {"role": "system", "content": "p" * 30},
            {"role": "user", "content": original},
        ]
        bounded = await service._bounded_messages(
            messages, max_tokens=20, request_id="latest",
        )
        compacted = bounded[-1]["content"]
        assert compacted.startswith("ANCHOR-")
        assert compacted.endswith("-CORRECTION")
        assert "[…]" in compacted
        assert sum(len(item["content"]) for item in bounded) <= 70
        assert messages[-1]["content"] == original

    async def test_overflow_compacts_auxiliary_system_before_latest(self) -> None:
        service = LlamaCppLLMService(
            context_size=100, context_safety_tokens=10,
            context_min_aux_chars=5, context_min_latest_chars=10,
        )
        self._char_counter(service)
        messages = [
            {"role": "system", "content": "p" * 30},
            {"role": "system", "content": "AUX-" + ("x" * 50) + "-END"},
            {"role": "user", "content": "latest-is-preserved"},
        ]
        bounded = await service._bounded_messages(
            messages, max_tokens=20, request_id="aux",
        )
        assert "[…]" in bounded[1]["content"]
        assert bounded[1]["content"].startswith("AUX-")
        assert bounded[1]["content"].endswith("-END")
        assert bounded[-1] == messages[-1]

    async def test_without_system_prefix_oldest_user_assistant_pair_is_droppable(self) -> None:
        service = LlamaCppLLMService(
            context_size=100, context_safety_tokens=10,
            context_min_aux_chars=5, context_min_latest_chars=10,
        )
        self._char_counter(service)
        messages = [
            {"role": "user", "content": "u" * 30},
            {"role": "assistant", "content": "a" * 30},
            {"role": "user", "content": "z" * 40},
        ]
        bounded = await service._bounded_messages(
            messages, max_tokens=20, request_id="no-prefix",
        )
        assert bounded == [messages[-1]]
        assert service.get_metrics()["llm_context_dropped_messages_total"] == 2

    async def test_counter_failure_is_fail_closed_before_generation_socket(self) -> None:
        service = LlamaCppLLMService()
        opened = False

        async def _counter(_messages):
            raise httpx.ConnectError("counter unavailable")

        async def _opener():
            nonlocal opened
            opened = True
            raise AssertionError("generation socket must remain closed")

        service._count_input_tokens = _counter  # type: ignore[assignment]
        service._open_connection = _opener  # type: ignore[assignment]
        with pytest.raises(LlamaCppError, match="token preflight failed"):
            _ = [token async for token in service.generate_stream(req())]
        assert opened is False
        metrics = service.get_metrics()
        assert metrics["llm_context_counter_failures_total"] == 1
        assert metrics["llm_errors_total"] == 1

    async def test_minimum_context_that_still_exceeds_budget_fails_closed(self) -> None:
        service = LlamaCppLLMService(
            context_size=50, context_safety_tokens=5,
            context_min_aux_chars=5, context_min_latest_chars=20,
        )
        self._char_counter(service)
        with pytest.raises(LlamaCppError, match="context budget unresolved"):
            await service._bounded_messages(
                [
                    {"role": "system", "content": "p" * 30},
                    {"role": "user", "content": "z" * 30},
                ],
                max_tokens=10,
                request_id="unresolved",
            )
        assert service.get_metrics()["llm_context_budget_failures_total"] == 1

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"context_size": True}, "context_size"),
            ({"context_safety_tokens": "32"}, "context_safety_tokens"),
            ({"context_min_aux_chars": 0}, "context_min_aux_chars"),
            ({"context_min_latest_chars": False}, "context_min_latest_chars"),
            ({"context_preflight_timeout_s": 3}, "context_preflight_timeout_s"),
        ],
    )
    def test_context_config_is_strict(self, kwargs: dict, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            LlamaCppLLMService(**kwargs)


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
        assert svc.default_max_tokens == 500  # bumped 2026-08-06 (Mai nói dài hơn)
        assert svc._host == "127.0.0.1"
        assert svc._port == 8080
        assert svc.context_size == 4096
        assert svc.context_safety_tokens == 32
        assert svc.context_min_aux_chars == 160
        assert svc.context_min_latest_chars == 512
        assert svc.context_preflight_timeout_s == 3.0
