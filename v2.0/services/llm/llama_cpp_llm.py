"""LlamaCppLLMService streaming through the llama.cpp server.

Cách gọi ĐÃ CHỐT (xem docs/MAI_V2_SYSTEM_SPEC.md + memory reference-llm-model):

- Endpoint `/v1/chat/completions` (KHÔNG `/completion` raw). Model là uncensored
  Gemma 4 12B, cần chat template của chính nó; /completion raw → output rác.
  Persona + history đi trong `messages` (do prompt_manager 1.C dựng).

- **Streaming qua `asyncio.open_connection` (raw socket stdlib), KHÔNG httpx.**
  httpx buffer SSE ~2.2s bất kể iter_lines/bytes/raw → raw socket TTFT ~72ms.
  Ta tự build HTTP POST + parse SSE tay. httpx chỉ dùng cho health và token-count
  preflight non-stream trước generation để khóa `n_ctx`.

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
import math
import time
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from interfaces.base import HealthStatus
from interfaces.llm import (
    LLMContextOverflowPolicy,
    LLMRequest,
    LLMService,
    LLMToken,
    LLMWorkloadClass,
)
from orchestrator.logger import get_logger

# Giới hạn dòng cho StreamReader (SSE delta nhỏ, nhưng nới rộng phòng câu dài).
_READ_LIMIT = 1024 * 1024
_COMPACTION_MARKER = "\n[…]\n"


class LlamaCppError(Exception):
    pass


class LlamaCppBusyError(LlamaCppError):
    pass


class LlamaCppContextBudgetError(LlamaCppError):
    pass


class LlamaCppPreemptedError(LlamaCppError):
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
        context_size: int = 4096,
        context_safety_tokens: int = 32,
        context_min_aux_chars: int = 160,
        context_min_latest_chars: int = 512,
        context_preflight_timeout_s: float = 3.0,
        shadow_cancel_grace_s: float = 0.25,
        metrics: Any = None,
    ) -> None:
        self.context_size = _strict_int(context_size, "context_size", minimum=2)
        self.context_safety_tokens = _strict_int(
            context_safety_tokens, "context_safety_tokens", minimum=0,
        )
        self.context_min_aux_chars = _strict_int(
            context_min_aux_chars, "context_min_aux_chars", minimum=1,
        )
        self.context_min_latest_chars = _strict_int(
            context_min_latest_chars, "context_min_latest_chars", minimum=1,
        )
        self.context_preflight_timeout_s = _strict_float(
            context_preflight_timeout_s, "context_preflight_timeout_s",
        )
        self.shadow_cancel_grace_s = _strict_float(
            shadow_cancel_grace_s, "shadow_cancel_grace_s",
        )
        if self.context_safety_tokens >= self.context_size - 1:
            raise ValueError("context_safety_tokens must leave at least one input token")
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
        # httpx is used for health and exact input-token preflight; generation
        # still uses the raw streaming socket below.
        self._client = client
        self._owns_client = client is None
        self._log = get_logger("llm")
        self._metrics = metrics
        self._cancelled: set[str] = set()
        self._active_writers: dict[str, asyncio.StreamWriter] = {}

        # metrics (per-request cập nhật, dashboard 1.F đọc)
        self._requests_total = 0
        self._errors_total = 0
        self._last_ttft_ms: float | None = None
        self._last_decode_tps: float | None = None
        self._last_tokens_out = 0
        self._context_preflight_total = 0
        self._context_compactions_total = 0
        self._context_dropped_messages_total = 0
        self._context_budget_failures_total = 0
        self._context_counter_failures_total = 0
        self._context_counter_calls_total = 0
        self._context_last_input_tokens: int | None = None
        self._live_active = 0
        self._shadow_active_request_id: str | None = None
        self._shadow_rejected_busy_total = 0
        self._shadow_preempted_total = 0
        self._workload_overlap_total = 0
        self._shadow_admission_enabled = True
        self._shadow_released = asyncio.Event()
        self._shadow_released.set()

    @classmethod
    def from_loader(
        cls, loader, client: httpx.AsyncClient | None = None, metrics: Any = None,
    ) -> LlamaCppLLMService:
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
            context_size=_strict_int(
                loader.get("models", "llm_main.context_size", 4096),
                "llm_main.context_size", minimum=2,
            ),
            context_safety_tokens=_strict_int(
                loader.get("models", "llm_main.context_safety_tokens", 32),
                "llm_main.context_safety_tokens", minimum=0,
            ),
            context_min_aux_chars=_strict_int(
                loader.get("models", "llm_main.context_min_aux_chars", 160),
                "llm_main.context_min_aux_chars", minimum=1,
            ),
            context_min_latest_chars=_strict_int(
                loader.get("models", "llm_main.context_min_latest_chars", 512),
                "llm_main.context_min_latest_chars", minimum=1,
            ),
            context_preflight_timeout_s=_strict_float(
                loader.get("models", "llm_main.context_preflight_timeout_s", 3.0),
                "llm_main.context_preflight_timeout_s",
            ),
            shadow_cancel_grace_s=_strict_float(
                loader.get("cognition", "brain_cancel_grace_seconds", 0.25),
                "brain_cancel_grace_seconds",
            ),
            metrics=metrics,
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
            "llm_context_preflight_total": self._context_preflight_total,
            "llm_context_compactions_total": self._context_compactions_total,
            "llm_context_dropped_messages_total": self._context_dropped_messages_total,
            "llm_context_budget_failures_total": self._context_budget_failures_total,
            "llm_context_counter_failures_total": self._context_counter_failures_total,
            "llm_context_counter_calls_total": self._context_counter_calls_total,
            "llm_context_last_input_tokens": self._context_last_input_tokens,
            "llm_live_active": self._live_active,
            "llm_shadow_active": self._shadow_active_request_id is not None,
            "llm_shadow_rejected_busy_total": self._shadow_rejected_busy_total,
            "llm_shadow_preempted_total": self._shadow_preempted_total,
            "llm_workload_overlap_total": self._workload_overlap_total,
            "llm_shadow_admission_enabled": self._shadow_admission_enabled,
        }

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.request_timeout_s)
        return self._client

    def set_metrics(self, metrics: Any) -> None:
        """Attach the runtime collector without changing legacy factory call sites."""
        self._metrics = metrics

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
        self._admit_workload(request)
        t_start = time.perf_counter()
        t_first: float | None = None
        tokens_out = 0
        writer: asyncio.StreamWriter | None = None

        try:
            if (
                request.workload_class is LLMWorkloadClass.LIVE
                and not self._shadow_released.is_set()
            ):
                try:
                    await asyncio.wait_for(
                        self._shadow_released.wait(),
                        timeout=self.shadow_cancel_grace_s,
                    )
                except asyncio.TimeoutError:
                    self._shadow_admission_enabled = False
                    self._workload_overlap_total += 1
                    _call_metric(
                        self._metrics, "record_llm_workload_overlap", "live_shadow",
                    )
            self._cancelled.discard(request.request_id)
            self._requests_total += 1
            max_tokens = request.max_tokens or self.default_max_tokens
            payload: dict[str, Any] = {
                "messages": request.to_messages(),
                "max_tokens": max_tokens,
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
            if request.response_format is not None:
                payload["response_format"] = request.response_format.to_payload()
            payload["messages"] = await self._bounded_messages(
                payload["messages"], max_tokens=max_tokens, request_id=request.request_id,
                overflow_policy=request.context_overflow_policy,
            )
            input_tokens = self._context_last_input_tokens
            reader, writer = await self._open_connection()
            self._active_writers[request.request_id] = writer
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
                        metadata={
                            "finish_reason": finish,
                            "tokens_predicted": tokens_out,
                            "input_tokens": input_tokens,
                        },
                    )
                    return
            if (
                request.workload_class is LLMWorkloadClass.SHADOW
                and request.request_id in self._cancelled
            ):
                raise LlamaCppPreemptedError("llama.cpp workload was preempted")
        except LlamaCppError as exc:
            if (
                request.workload_class is LLMWorkloadClass.SHADOW
                and request.request_id in self._cancelled
                and not isinstance(exc, LlamaCppPreemptedError)
            ):
                raise LlamaCppPreemptedError(
                    "llama.cpp workload was preempted",
                ) from exc
            raise
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            self._errors_total += 1
            raise LlamaCppError(f"timeout sau {self.request_timeout_s}s") from e
        except Exception as e:
            self._errors_total += 1
            raise LlamaCppError(f"generate_stream failed: {e}") from e
        finally:
            self._cancelled.discard(request.request_id)
            self._active_writers.pop(request.request_id, None)
            if writer is not None:
                writer.close()
            self._record_decode(t_first, tokens_out)
            self._release_workload(request)

    async def _bounded_messages(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        request_id: str,
        overflow_policy: LLMContextOverflowPolicy = LLMContextOverflowPolicy.COMPACT,
    ) -> list[dict[str, str]]:
        """Return a copied message list proven to fit the configured context budget."""
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            self._context_budget_failures_total += 1
            self._errors_total += 1
            raise LlamaCppError("context budget requires strict positive max_tokens")
        budget = self.context_size - max_tokens - self.context_safety_tokens
        if budget < 1:
            self._context_budget_failures_total += 1
            self._errors_total += 1
            raise LlamaCppError(
                f"context budget invalid: n_ctx={self.context_size}, output={max_tokens}, "
                f"safety={self.context_safety_tokens}"
            )

        self._context_preflight_total += 1
        working = [dict(message) for message in messages]
        tokens = await self._checked_input_tokens(working)
        self._context_last_input_tokens = tokens
        if tokens <= budget:
            return working

        initial_tokens = tokens
        if overflow_policy is LLMContextOverflowPolicy.REJECT:
            self._context_budget_failures_total += 1
            self._errors_total += 1
            raise LlamaCppContextBudgetError(
                f"context budget exceeded without compaction: input={tokens}, budget={budget}"
            )
        self._context_compactions_total += 1

        # Drop only middle conversational history. The first stable system prefix
        # and latest instruction/user message remain owned by their callers.
        prefix_count = 1 if working and working[0].get("role") == "system" else 0
        while len(working) > prefix_count + 1 and tokens > budget:
            index = next(
                (i for i in range(prefix_count, len(working) - 1)
                 if working[i].get("role") != "system"),
                None,
            )
            if index is None:
                break
            remove_count = 1
            if (
                working[index].get("role") == "user"
                and index + 1 < len(working) - 1
                and working[index + 1].get("role") == "assistant"
            ):
                remove_count = 2
            del working[index:index + remove_count]
            self._context_dropped_messages_total += remove_count
            tokens = await self._checked_input_tokens(working)

        # Auxiliary system context is lower priority than the latest grounded
        # instruction. Compact its middle while retaining both ends.
        for index in range(prefix_count, len(working) - 1):
            if tokens <= budget:
                break
            if working[index].get("role") != "system":
                continue
            working, tokens = await self._fit_message(
                working, index, budget, self.context_min_aux_chars,
            )

        if tokens > budget and working:
            working, tokens = await self._fit_message(
                working, len(working) - 1, budget, self.context_min_latest_chars,
            )

        self._context_last_input_tokens = tokens
        if tokens > budget:
            self._context_budget_failures_total += 1
            self._errors_total += 1
            raise LlamaCppError(
                f"context budget unresolved: input={tokens}, budget={budget}, "
                f"n_ctx={self.context_size}"
            )
        self._log.info(
            "llm_context_compacted",
            request_id=request_id,
            input_tokens=initial_tokens,
            compacted_tokens=tokens,
            budget_tokens=budget,
        )
        return working

    async def _fit_message(
        self,
        messages: list[dict[str, str]],
        index: int,
        budget: int,
        minimum_chars: int,
    ) -> tuple[list[dict[str, str]], int]:
        original = str(messages[index].get("content", ""))
        if len(original) <= minimum_chars:
            return messages, await self._checked_input_tokens(messages)

        low = min(minimum_chars, len(original))
        high = len(original) - 1
        best_messages: list[dict[str, str]] | None = None
        best_tokens: int | None = None

        minimum_candidate = [dict(message) for message in messages]
        minimum_candidate[index]["content"] = _compact_middle(original, low)
        minimum_tokens = await self._checked_input_tokens(minimum_candidate)
        if minimum_tokens > budget:
            return minimum_candidate, minimum_tokens
        best_messages, best_tokens = minimum_candidate, minimum_tokens

        while low <= high:
            retain = (low + high) // 2
            candidate = [dict(message) for message in messages]
            candidate[index]["content"] = _compact_middle(original, retain)
            candidate_tokens = await self._checked_input_tokens(candidate)
            if candidate_tokens <= budget:
                best_messages, best_tokens = candidate, candidate_tokens
                low = retain + 1
            else:
                high = retain - 1
        return best_messages, int(best_tokens)

    async def _checked_input_tokens(self, messages: list[dict[str, str]]) -> int:
        self._context_counter_calls_total += 1
        try:
            value = await self._count_input_tokens(messages)
        except Exception as exc:
            self._context_counter_failures_total += 1
            self._errors_total += 1
            raise LlamaCppError(f"context token preflight failed: {exc}") from exc
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            self._context_counter_failures_total += 1
            self._errors_total += 1
            raise LlamaCppError("context token preflight returned invalid input_tokens")
        return value

    async def _count_input_tokens(self, messages: list[dict[str, str]]) -> int:
        client = self._ensure_client()
        response = await client.post(
            f"{self.base_url}/v1/chat/completions/input_tokens",
            json={"messages": messages},
            timeout=self.context_preflight_timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("input_tokens")

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
        """Mark cancellation and close the owned socket to release llama.cpp promptly."""
        self._cancelled.add(request_id)
        writer = self._active_writers.get(request_id)
        if writer is not None:
            writer.close()

    def _admit_workload(self, request: LLMRequest) -> None:
        if request.workload_class is LLMWorkloadClass.SHADOW:
            if (
                not self._shadow_admission_enabled
                or self._live_active > 0
                or self._shadow_active_request_id is not None
            ):
                self._shadow_rejected_busy_total += 1
                raise LlamaCppBusyError("shadow workload rejected while llama.cpp is busy")
            self._shadow_active_request_id = request.request_id
            self._shadow_released.clear()
            return
        self._live_active += 1
        shadow_id = self._shadow_active_request_id
        if shadow_id is not None:
            self._shadow_preempted_total += 1
            self._cancelled.add(shadow_id)
            writer = self._active_writers.get(shadow_id)
            if writer is not None:
                writer.close()

    def _release_workload(self, request: LLMRequest) -> None:
        if request.workload_class is LLMWorkloadClass.SHADOW:
            if self._shadow_active_request_id == request.request_id:
                self._shadow_active_request_id = None
                self._shadow_released.set()
            return
        self._live_active = max(0, self._live_active - 1)


def _call_metric(metrics: Any, method: str, *args: Any) -> None:
    recorder = getattr(metrics, method, None)
    if not callable(recorder):
        return
    try:
        recorder(*args)
    except Exception:
        pass


def _strict_int(value: Any, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field_name} must be a strict integer >= {minimum}")
    return value


def _strict_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be a strict finite positive float")
    return value


def _compact_middle(content: str, retained_chars: int) -> str:
    if retained_chars >= len(content):
        return content
    retained_chars = max(0, retained_chars)
    head_chars = (retained_chars + 1) // 2
    tail_chars = retained_chars // 2
    head = content[:head_chars].rstrip()
    tail = content[-tail_chars:].lstrip() if tail_chars else ""
    return f"{head}{_COMPACTION_MARKER}{tail}"
