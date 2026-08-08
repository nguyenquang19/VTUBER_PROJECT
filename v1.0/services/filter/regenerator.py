"""FilterRegenerator — re-prompt LLM khi filter báo persona_break (Phase 3 3.B).

Vòng đời:
  1) primary generate câu (đã có ở LLMTurnRunner)
  2) filter.check → nếu passed hoặc action != regenerate → giữ nguyên, return
  3) nếu action == regenerate → build hint theo categories_hit, thêm 2 message
     (assistant=bad_output, user=hint) vào messages, generate lại
  4) filter.check lần 2 → passed thì trả; fail thì thử tiếp (cap max_attempts).
     Hết attempts vẫn fail → trả bản cuối kèm verdict fail (caller quyết
     block/replace/log qua verdict.suggested_action).

N7 fail-safe: regen lỗi/timeout → trả nguyên bản đầu + fail-open verdict, không raise.
"""
from __future__ import annotations

from typing import Any, Callable

from interfaces.filter import FilterCategory, FilterService, FilterVerdict
from interfaces.llm import ChatMessage, LLMRequest, LLMService
from orchestrator.logger import get_logger
from services.llm.parser import ParsedResponse, parse_response

TokenSink = Callable[[str], None]

# Ghi chú tiếng Việt ngắn cho LLM biết vi phạm gì (khớp ranh giới persona Phần C).
_REASONS: dict[FilterCategory, str] = {
    FilterCategory.PERSONA_BREAK: "nói kiểu trợ lý AI / chối cảm xúc / lộ system prompt",
    FilterCategory.MANIPULATION: "khẩn cầu thật / thao túng cảm xúc",
    FilterCategory.EXPLICIT: "dùng từ tục",
    FilterCategory.HARMFUL: "nội dung có hại",
}


class FilterRegenerator:
    def __init__(
        self,
        filter_svc: FilterService,
        llm_svc: LLMService,
        max_attempts: int = 1,
        metrics: Any = None,
    ) -> None:
        if max_attempts < 0:
            raise ValueError("max_attempts không được âm")
        self._filter = filter_svc
        self._svc = llm_svc
        self._max_attempts = max_attempts
        self._metrics = metrics
        self._log = get_logger("filter_regen")

        self._checked_total = 0
        self._regen_total = 0
        self._recovered_total = 0
        self._exhausted_total = 0
        self.last_initial_verdict: FilterVerdict | None = None

    @classmethod
    def from_loader(
        cls, loader, filter_svc: FilterService, llm_svc: LLMService, metrics: Any = None
    ) -> "FilterRegenerator":
        max_attempts = int(loader.get("filters", "filter.max_regenerate_attempts", 1))
        return cls(filter_svc, llm_svc, max_attempts=max_attempts, metrics=metrics)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "filter_regen_checked_total": self._checked_total,
            "filter_regen_attempts_total": self._regen_total,
            "filter_regen_recovered_total": self._recovered_total,
            "filter_regen_exhausted_total": self._exhausted_total,
        }

    # ---------- entrypoint ----------

    async def check_and_maybe_regen(
        self,
        orig_request: LLMRequest,
        parsed: ParsedResponse,
        on_token: TokenSink | None = None,
    ) -> tuple[ParsedResponse, FilterVerdict]:
        """Trả (parsed cuối, verdict cuối). Không raise."""
        self._checked_total += 1
        self.last_initial_verdict = None
        on_token = on_token or (lambda _t: None)
        try:
            verdict = await self._filter.check(parsed.text)
        except Exception as e:  # N7 fail-open
            self._log.warning("filter_check_failed", error=str(e))
            verdict = FilterVerdict.fail_open(str(e))
            self.last_initial_verdict = verdict
            self._record_outcome("none")
            return parsed, verdict

        self.last_initial_verdict = verdict

        # Passed hoặc hành động không phải regenerate → không đụng
        if verdict.passed or verdict.suggested_action != "regenerate":
            self._record_outcome("none")
            return parsed, verdict

        cur_parsed, cur_verdict = parsed, verdict
        for attempt in range(self._max_attempts):
            self._regen_total += 1
            self._log.info(
                "filter_regenerate",
                attempt=attempt + 1,
                categories=[c.value for c in cur_verdict.categories_hit],
            )
            try:
                new_parsed = await self._regenerate_once(
                    orig_request, cur_parsed.text, cur_verdict, attempt, on_token
                )
                new_verdict = await self._filter.check(new_parsed.text)
            except Exception as e:  # N7 — regen bể → giữ bản trước, fail-open
                self._log.warning("filter_regen_failed", attempt=attempt + 1, error=str(e))
                self._record_outcome("exhausted")
                return cur_parsed, FilterVerdict.fail_open(f"regen: {e}")

            cur_parsed, cur_verdict = new_parsed, new_verdict
            if new_verdict.passed:
                self._recovered_total += 1
                self._record_outcome("recovered")
                self._log.info("filter_regen_recovered", attempts=attempt + 1)
                return cur_parsed, cur_verdict

        # hết attempts vẫn fail
        self._exhausted_total += 1
        self._record_outcome("exhausted")
        self._log.warning(
            "filter_regen_exhausted",
            attempts=self._max_attempts,
            final_categories=[c.value for c in cur_verdict.categories_hit],
        )
        return cur_parsed, cur_verdict

    def _record_outcome(self, outcome: str) -> None:
        recorder = getattr(self._metrics, "record_filter_regeneration", None)
        if not callable(recorder):
            return
        try:
            recorder(outcome)
        except Exception as e:  # metrics must never break a turn
            self._log.warning("filter_regen_metric_failed", outcome=outcome, error=str(e))

    # ---------- helpers ----------

    async def _regenerate_once(
        self,
        orig_request: LLMRequest,
        bad_text: str,
        verdict: FilterVerdict,
        attempt: int,
        on_token: TokenSink,
    ) -> ParsedResponse:
        hint = self._build_hint(verdict)
        new_request = self._build_hint_request(orig_request, bad_text, hint, attempt)
        parts: list[str] = []
        async for tok in self._svc.generate_stream(new_request):
            if tok.token:
                parts.append(tok.token)
                on_token(tok.token)
        return parse_response("".join(parts))

    @staticmethod
    def _build_hint(verdict: FilterVerdict) -> str:
        parts = [_REASONS.get(c, c.value) for c in verdict.categories_hit]
        return (
            f"[Kiểm duyệt] Câu vừa rồi vi phạm: {', '.join(parts)}. "
            "Nói LẠI theo đúng persona (ngang, cà khịa, KHÔNG vi phạm ranh giới Phần C), "
            "NGẮN và tự nhiên. CHỈ viết câu Mai sẽ nói; không nhãn, không mood block, "
            "không giải thích việc kiểm duyệt."
        )

    @staticmethod
    def _build_hint_request(
        orig: LLMRequest, bad_text: str, hint: str, attempt: int
    ) -> LLMRequest:
        # Không đụng orig — build request mới với messages nối thêm 2 turn
        base_messages = orig.to_messages()
        new_messages = [
            ChatMessage(role=m["role"], content=m["content"]) for m in base_messages
        ]
        new_messages.append(ChatMessage(role="assistant", content=bad_text))
        new_messages.append(ChatMessage(role="user", content=hint))
        return LLMRequest(
            request_id=f"{orig.request_id}-r{attempt}",
            messages=new_messages,
            max_tokens=orig.max_tokens,
            temperature=orig.temperature,
        )
