"""LlamaCppLLMService — streaming qua llama-server (ARCHITECTURE 8.2, 1.B).

Cách gọi ĐÃ CHỐT (xem docs/MAI_V2_SYSTEM_SPEC.md + memory reference-llm-model):

- Endpoint `/v1/chat/completions` (KHÔNG `/completion` raw). Model là uncensored
  Gemma 4 12B, cần chat template của chính nó; /completion raw → output rác.
  Persona + history đi trong `messages` (do prompt_manager 1.C dựng).

- **Streaming qua `asyncio.open_connection` (raw socket stdlib), KHÔNG httpx.**
  httpx buffer SSE ~2.2s bất kể iter_lines/bytes/raw → raw socket TTFT ~72ms.
  Ta tự build HTTP POST + parse SSE tay. httpx CHỈ dùng cho health (non-stream OK).

- Server phải chạy với `--reasoning off` (reasoning là native của Gemma 4). Với
  flag đó, delta trả `content` thẳng. Vẫn đọc thêm `reasoning_content` phòng khi
  rò (parser 1.D lo tách sạch); ở đây chỉ cần lấy được text answer.

- `cache_prompt: true` trong request → server giữ KV cache prefix persona giữa
  các turn (prompt caching thật, thay flag --prompt-cache không tồn tại — 1.A).

Interface: implement LLMService (interfaces/llm.py).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from interfaces.base import HealthStatus
from interfaces.llm import LLMRequest, LLMService, LLMToken
from orchestrator.logger import get_logger

# Giới hạn dòng cho StreamReader (SSE delta nhỏ, nhưng nới rộng phòng câu dài).
_READ_LIMIT = 1024 * 1024


class LlamaCppError(Exception):
    pass


class LlamaCppLLMService(LLMService):
    service_id = "llm_main"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        default_max_tokens: int = 300,
        request_timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
        sampling: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or 8080
        self.default_max_tokens = default_max_tokens
        self.request_timeout_s = request_timeout_s
        # Sampling register toàn cục (min_p/repeat_penalty/presence...) — chỉ giữ
        # key có giá trị non-null để không đè default llama-server bằng None.
        self._sampling: dict[str, Any] = {
            k: v for k, v in (sampling or {}).items() if v is not None
        }
        self._client = client  # httpx — CHỈ cho health, KHÔNG cho stream
        self._owns_client = client is None
        self._log = get_logger("llm")
        self._cancelled: set[str] = set()

        # metrics (per-request cập nhật, dashboard 1.F đọc)
        self._requests_total = 0
        self._errors_total = 0
        self._last_ttft_ms: float | None = None
        self._last_decode_tps: float | None = None
        self._last_tokens_out = 0

    @classmethod
    def from_loader(cls, loader, client: httpx.AsyncClient | None = None) -> LlamaCppLLMService:
        host = loader.get("models", "llm_main.host", "127.0.0.1")
        port = int(loader.get("models", "llm_main.port", 8080))
        # Sampling register — đọc từ config, key thiếu → None (không gửi).
        sampling = {
            "min_p": loader.get("models", "llm_main.min_p", None),
            "top_p": loader.get("models", "llm_main.top_p", None),
            "top_k": loader.get("models", "llm_main.top_k", None),
            "repeat_penalty": loader.get("models", "llm_main.repeat_penalty", None),
            "repeat_last_n": loader.get("models", "llm_main.repeat_last_n", None),
            "presence_penalty": loader.get("models", "llm_main.presence_penalty", None),
            "frequency_penalty": loader.get("models", "llm_main.frequency_penalty", None),
        }
        return cls(
            base_url=f"http://{host}:{port}",
            default_max_tokens=int(loader.get("models", "llm_main.num_predict", 300)),
            client=client,
            sampling=sampling,
        )

    # ---------- Service ----------

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.request_timeout_s)

    async def stop(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> HealthStatus:
        try:
            client = self._ensure_client()
            r = await client.get(f"{self.base_url}/health", timeout=3)
            if r.status_code == 200:
                return HealthStatus.healthy(self.service_id, base_url=self.base_url)
            return HealthStatus.unhealthy(self.service_id, f"health HTTP {r.status_code}")
        except Exception as e:
            return HealthStatus.unhealthy(self.service_id, f"unreachable: {e}")

    def get_metrics(self) -> dict[str, Any]:
        return {
            "llm_requests_total": self._requests_total,
            "llm_errors_total": self._errors_total,
            "llm_last_ttft_ms": self._last_ttft_ms,
            "llm_last_decode_tps": self._last_decode_tps,
            "llm_last_tokens_out": self._last_tokens_out,
        }

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.request_timeout_s)
        return self._client

    # ---------- generation (raw socket, KHÔNG httpx) ----------

    async def _open_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Mở TCP tới llama-server. Tách riêng để test inject fake reader."""
        return await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port, limit=_READ_LIMIT),
            timeout=self.request_timeout_s,
        )

    def _build_http_request(self, payload: dict) -> bytes:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        host_hdr = f"{self._host}:{self._port}".encode("ascii")
        return (
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: " + host_hdr + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Accept: text/event-stream\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )

    async def generate_stream(self, request: LLMRequest) -> AsyncIterator[LLMToken]:
        self._cancelled.discard(request.request_id)
        self._requests_total += 1

        payload: dict[str, Any] = {
            "messages": request.to_messages(),
            "max_tokens": request.max_tokens or self.default_max_tokens,
            "temperature": request.temperature,
            "stream": True,
            "cache_prompt": True,
        }
        if request.seed is not None:
            payload["seed"] = request.seed
        # Sampling register toàn cục (min_p/repeat_penalty/presence...) — de-AI giọng.
        payload.update(self._sampling)
        if request.stop_sequences:
            payload["stop"] = request.stop_sequences

        t_start = time.perf_counter()
        t_first: float | None = None
        tokens_out = 0
        writer: asyncio.StreamWriter | None = None

        try:
            reader, writer = await self._open_connection()
            writer.write(self._build_http_request(payload))
            await writer.drain()

            status = await self._read_status(reader)
            if status >= 400:
                body = await self._read_rest(reader)
                self._errors_total += 1
                raise LlamaCppError(f"HTTP {status}: {body[:300]}")

            async for obj in self._iter_sse(reader, request.request_id):
                choice = (obj.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                content = delta.get("content") or delta.get("reasoning_content") or ""
                finish = choice.get("finish_reason")
                if content:
                    if t_first is None:
                        t_first = time.perf_counter()
                        self._last_ttft_ms = (t_first - t_start) * 1000
                    tokens_out += 1
                    yield LLMToken(
                        request_id=request.request_id,
                        token=content,
                        is_final=False,
                    )
                if finish:
                    yield LLMToken(
                        request_id=request.request_id,
                        token="",
                        is_final=True,
                        metadata={"finish_reason": finish, "tokens_predicted": tokens_out},
                    )
                    return
        except (LlamaCppError, asyncio.CancelledError):
            raise
        except asyncio.TimeoutError as e:
            self._errors_total += 1
            raise LlamaCppError(f"timeout sau {self.request_timeout_s}s") from e
        except Exception as e:
            self._errors_total += 1
            raise LlamaCppError(f"generate_stream failed: {e}") from e
        finally:
            self._cancelled.discard(request.request_id)
            if writer is not None:
                writer.close()
            self._record_decode(t_first, tokens_out)

    async def _readline(self, reader: asyncio.StreamReader) -> bytes:
        return await asyncio.wait_for(reader.readline(), timeout=self.request_timeout_s)

    async def _read_status(self, reader: asyncio.StreamReader) -> int:
        """Đọc status line + tiêu đề tới dòng trống. Trả HTTP status code."""
        status_line = await self._readline(reader)
        if not status_line:
            raise LlamaCppError("kết nối đóng trước khi có response")
        parts = status_line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise LlamaCppError(f"status line lạ: {status_line!r}")
        code = int(parts[1])
        while True:  # nuốt hết header tới dòng trống
            line = await self._readline(reader)
            if line in (b"\r\n", b"\n", b""):
                break
        return code

    async def _read_rest(self, reader: asyncio.StreamReader) -> str:
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=self.request_timeout_s)
        except asyncio.TimeoutError:
            data = b""
        return data.decode("utf-8", "replace")

    async def _iter_sse(
        self, reader: asyncio.StreamReader, request_id: str
    ) -> AsyncIterator[dict]:
        """Đọc body SSE dòng-theo-dòng, yield JSON object của mỗi `data:`.

        Body có thể là chunked transfer — dòng kích thước chunk (hex) không chứa
        "data:" nên bị bỏ qua tự nhiên; readline tự gộp dòng bị cắt ngang.
        """
        while True:
            if request_id in self._cancelled:
                self._log.info("llm_cancelled", request_id=request_id)
                return
            line = await self._readline(reader)
            if not line:  # EOF
                return
            s = line.decode("utf-8", "replace")
            idx = s.find("data:")
            if idx == -1:
                continue
            data = s[idx + 5:].strip()
            if not data:
                continue
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue

    def _record_decode(self, t_first: float | None, tokens_out: int) -> None:
        self._last_tokens_out = tokens_out
        if t_first is not None and tokens_out > 1:
            decode_s = time.perf_counter() - t_first
            self._last_decode_tps = tokens_out / decode_s if decode_s > 0 else None

    async def cancel(self, request_id: str) -> None:
        """Đánh dấu cancel; _iter_sse sẽ dừng ở vòng kế → đóng socket ở finally."""
        self._cancelled.add(request_id)
