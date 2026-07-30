"""LLMTurnRunner — chạy 1 lượt Mai trả lời qua fallback chain (1.E).

Ghép 1.B (stream) + 1.C (prompt) + 1.D (parse) + fallback (0.D) + canned (1.E):

  build_request (PromptManager)
    → FallbackManager.execute("llm", request):
         Level 0 (primary): stream token (in ra qua on_token) + parse_response
         Level 1 (canned):  CannedResponder.build() theo mood gần nhất
    → commit_turn vào history (lưu text ĐÃ tách mood block)

N7 fail-safe: primary lỗi/timeout → tự rơi xuống canned, không crash.
N8: dùng lại FallbackManager generic, không tự viết vòng retry.
"""
from __future__ import annotations

from typing import Any, Callable

from interfaces.llm import LLMService
from orchestrator.fallback_manager import FallbackManager
from services.llm.canned_response import CannedResponder
from services.llm.parser import ParsedResponse, parse_response
from services.llm.prompt_manager import PromptManager

_CHAIN_ID = "llm"
TokenSink = Callable[[str], None]


class LLMTurnRunner:
    def __init__(
        self,
        svc: LLMService,
        prompt_manager: PromptManager,
        fallback: FallbackManager,
        canned: CannedResponder,
        timeout_primary_s: float = 5.0,
        timeout_canned_s: float = 0.1,
        on_token: TokenSink | None = None,
        metrics: Any = None,
    ) -> None:
        self._svc = svc
        self._pm = prompt_manager
        self._fb = fallback
        self._canned = canned
        self._on_token = on_token or (lambda _t: None)
        self._metrics = metrics
        self._fb.register_chain(
            _CHAIN_ID,
            [self._primary, self._canned_handler],
            [timeout_primary_s, timeout_canned_s],
        )

    @classmethod
    def from_loader(
        cls,
        loader,
        svc: LLMService,
        prompt_manager: PromptManager,
        fallback: FallbackManager,
        canned: CannedResponder,
        on_token: TokenSink | None = None,
        metrics: Any = None,
    ) -> "LLMTurnRunner":
        return cls(
            svc,
            prompt_manager,
            fallback,
            canned,
            timeout_primary_s=float(loader.get("models", "llm_canned.timeout_primary_s", 5.0)),
            timeout_canned_s=float(loader.get("models", "llm_canned.timeout_canned_s", 0.1)),
            on_token=on_token,
            metrics=metrics,
        )

    async def _primary(self, request: Any) -> ParsedResponse:
        parts: list[str] = []
        async for tok in self._svc.generate_stream(request):
            if tok.token:
                parts.append(tok.token)
                self._on_token(tok.token)
        return parse_response("".join(parts))

    async def _canned_handler(self, request: Any) -> ParsedResponse:
        parsed = self._canned.build()
        self._on_token(parsed.text)
        return parsed

    async def run_turn(self, request_id: str, user_text: str) -> tuple[ParsedResponse, int]:
        """Trả (parsed, level_used). level_used=0 primary, 1 canned."""
        request = self._pm.build_request(request_id, user_text)
        result = await self._fb.execute(_CHAIN_ID, request)
        parsed: ParsedResponse = result.value
        self._pm.commit_turn(user_text, parsed.text)
        if parsed.ok:
            self._canned.update_mood(parsed.mood)
        self._record_metrics(parsed, result.level_used)
        return parsed, result.level_used

    def _record_metrics(self, parsed: ParsedResponse, level_used: int) -> None:
        if self._metrics is None:
            return
        m: dict[str, Any] = {}
        get_metrics = getattr(self._svc, "get_metrics", None)
        if callable(get_metrics):
            try:
                m = get_metrics()
            except Exception:  # pragma: no cover - metrics best-effort
                m = {}
        self._metrics.record_llm_turn(
            ttft_ms=m.get("llm_last_ttft_ms"),
            decode_tps=m.get("llm_last_decode_tps"),
            parse_ok=parsed.ok,
            level_used=level_used,
        )
