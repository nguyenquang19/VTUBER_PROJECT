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

from interfaces.filter import FilterVerdict
from interfaces.llm import LLMService
from orchestrator.fallback_manager import FallbackManager
from orchestrator.logger import get_logger
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
        regenerator: Any = None,
        memory: Any = None,             # MemoryService (Phase 7.F)
        memory_extractor: Any = None,   # MemoryExtractor (Phase 7.F)
        emotion: Any = None,            # EmotionOrchestrator (Phase 7.5.E)
        drift_detector: Any = None,     # DriftDetector (Phase 7.5.E)
    ) -> None:
        self._svc = svc
        self._pm = prompt_manager
        self._fb = fallback
        self._canned = canned
        self._on_token = on_token or (lambda _t: None)
        self._metrics = metrics
        self._regen = regenerator  # FilterRegenerator | None (3.B)
        self._memory = memory
        self._memory_extractor = memory_extractor
        self._emotion = emotion
        self._drift = drift_detector
        self.last_filter_verdict: FilterVerdict | None = None
        self.last_drift_report: Any = None
        self._memory_writes_scheduled = 0
        self._memory_writes_skipped = 0
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
        regenerator: Any = None,
        memory: Any = None,
        memory_extractor: Any = None,
        emotion: Any = None,
        drift_detector: Any = None,
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
            regenerator=regenerator,
            memory=memory,
            memory_extractor=memory_extractor,
            emotion=emotion,
            drift_detector=drift_detector,
        )

    async def _primary(self, request: Any) -> ParsedResponse:
        parts: list[str] = []
        async for tok in self._svc.generate_stream(request):
            if tok.token:
                parts.append(tok.token)
                self._on_token(tok.token)
        parsed = parse_response("".join(parts))

        # 3.B: filter+regen (optional). Nếu bad → regen thay parsed; verdict lộ ra
        # last_filter_verdict để caller (dashboard/QC) đọc.
        if self._regen is not None:
            parsed, verdict = await self._regen.check_and_maybe_regen(
                request, parsed, on_token=self._on_token
            )
            self.last_filter_verdict = verdict
        return parsed

    async def _canned_handler(self, request: Any) -> ParsedResponse:
        parsed = self._canned.build()
        self._on_token(parsed.text)
        return parsed

    async def run_turn(
        self,
        request_id: str,
        user_text: str,
        viewer_id: str | None = None,
        session_id: str | None = None,
        trigger_type: str | None = None,
        event_category: str | None = None,
    ) -> tuple[ParsedResponse, int]:
        """Trả (parsed, level_used). level_used=0 primary, 1 canned.

        Nếu emotion orchestrator wire: peek current_mood + active_flags → dùng
        build_request_with_mood; sau turn apply_llm_hint (Kênh B turn kế) +
        drift detect + clear tone flags (Phase 7.5.E, spec Mục 6).
        """
        request = self._build_request_maybe_with_mood(request_id, user_text, event_category)
        # Snapshot mood ĐƯỢC GIAO (trước LLM) để drift detect sau
        engine_mood_pre = self._emotion.current_mood() if self._emotion is not None else None

        result = await self._fb.execute(_CHAIN_ID, request)
        parsed: ParsedResponse = result.value
        self._pm.commit_turn(user_text, parsed.text)
        if parsed.ok:
            self._canned.update_mood(parsed.mood)
        self._record_metrics(parsed, result.level_used)

        # Phase 7.5.E: LLM mood → Kênh B nudge turn kế + drift detect
        self._apply_emotion_feedback(parsed, engine_mood_pre)

        # Phase 7.F: auto-extract memory từ turn (fire-and-forget)
        self._schedule_memory_write(user_text, parsed, viewer_id, session_id, trigger_type)
        return parsed, result.level_used

    async def run_ambient_turn(self, request_id: str, prompt_text: str) -> ParsedResponse:
        """Mai tự nói (Autonomy Engine v2 — Aut.D wire).

        Khác run_turn thường:
        - user_text = prompt_text đã slot-fill sẵn từ AutonomyEngine (Context Mai
          tự lên tiếng, lý do, seed, forbidden opener…)
        - KHÔNG commit vào history (ambient không phải trao đổi user — commit sẽ
          bloat context nhanh khi silence dài)
        - Vẫn qua fallback chain (canned nếu timeout)
        - Vẫn feed emotion Kênh B (mood LLM tự report → apply_llm_hint)
        - KHÔNG memory_extract (ambient không có user_text để extract preference)
        """
        request = self._pm.build_request(request_id, prompt_text)
        result = await self._fb.execute(_CHAIN_ID, request)
        parsed: ParsedResponse = result.value
        if parsed.ok:
            self._canned.update_mood(parsed.mood)
        self._record_metrics(parsed, result.level_used)
        # Emotion feedback: apply_llm_hint (Kênh B) + clear tone flags
        if self._emotion is not None and parsed.ok:
            try:
                self._emotion.apply_llm_hint(parsed.mood)
            except Exception as e:
                get_logger("llm_turn").warning("ambient_emotion_hint_failed", error=str(e))
            try:
                self._emotion.clear_tone_flags()
            except Exception:
                pass
        return parsed

    def _build_request_maybe_with_mood(
        self, request_id: str, user_text: str, event_category: str | None,
    ):
        if self._emotion is None:
            return self._pm.build_request(request_id, user_text)
        return self._pm.build_request_with_mood(
            request_id=request_id,
            user_text=user_text,
            current_mood=self._emotion.current_mood(),
            event_category=event_category,
            tone_flags=self._emotion.active_tone_flags(),
        )

    def _apply_emotion_feedback(self, parsed: ParsedResponse, engine_mood_pre) -> None:
        if self._emotion is None or not parsed.ok:
            return
        # Kênh B: nudge target theo LLM self-report cho turn kế
        try:
            self._emotion.apply_llm_hint(parsed.mood)
        except Exception as e:
            get_logger("llm_turn").warning("emotion_hint_failed", error=str(e))
        # Drift detect (nếu có) — dùng engine_mood_pre (mood đã GIAO trước LLM)
        if self._drift is not None and engine_mood_pre is not None:
            try:
                self.last_drift_report = self._drift.detect(engine_mood_pre, parsed.mood)
            except Exception as e:
                get_logger("llm_turn").warning("drift_detect_failed", error=str(e))
        # Clear tone flags sau khi Prompt đã đọc (1 lần/turn)
        try:
            self._emotion.clear_tone_flags()
        except Exception:
            pass

    def _schedule_memory_write(
        self,
        user_text: str,
        parsed: ParsedResponse,
        viewer_id: str | None,
        session_id: str | None,
        trigger_type: str | None,
    ) -> None:
        if self._memory is None or self._memory_extractor is None:
            return
        # Build TurnData local để tránh phụ thuộc circular
        from services.memory.extractor import TurnData

        dominant_name, dominant_val = _dominant_mood(parsed)
        turn = TurnData(
            user_input=user_text,
            mai_output=parsed.text,
            mood_dominant=dominant_name,
            mood_intensity=dominant_val,
            viewer_id=viewer_id,
            session_id=session_id,
            trigger_type=trigger_type,
        )
        entry = self._memory_extractor.extract(turn)
        if entry is None:
            self._memory_writes_skipped += 1
            return
        # asyncio.create_task = fire-and-forget; nếu write lỗi, N7 fail-safe (memory
        # service log warning, không giết turn). Không await ở đây.
        try:
            import asyncio
            asyncio.get_running_loop().create_task(
                self._memory.write(entry), name=f"memory_write_{entry.entry_id[:8]}"
            )
            self._memory_writes_scheduled += 1
        except RuntimeError:
            # không có event loop (test sync) → skip
            self._memory_writes_skipped += 1

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
        # 3.C: forward filter verdict (nếu có regen) vào metrics
        recorder = getattr(self._metrics, "record_filter_check", None)
        if callable(recorder) and self.last_filter_verdict is not None:
            v = self.last_filter_verdict
            recorder(
                passed=v.passed,
                categories=[c.value for c in v.categories_hit],
                action=v.suggested_action,
                fail_open=v.reason.startswith("fail-open"),
            )


def _dominant_mood(parsed: ParsedResponse) -> tuple[str | None, int | None]:
    """Trả (name, intensity) mood cao nhất, hoặc (None, None) nếu parse fail."""
    if not parsed.ok or parsed.mood is None:
        return None, None
    name = parsed.mood.dominant()
    if name == "neutral":
        return name, 0
    val = getattr(parsed.mood, name, 0)
    return name, int(val)
