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

import re
import time
from typing import Any, Callable

from interfaces.filter import FilterVerdict
from interfaces.llm import LLMService
from orchestrator.fallback_manager import FallbackManager
from orchestrator.logger import TurnLogger, get_logger
from services.llm.canned_response import CannedResponder
from services.llm.parser import ParsedResponse, parse_response
from services.llm.prompt_manager import PromptManager

_CHAIN_ID = "llm"
TokenSink = Callable[[str], None]

# A1.1: detect mood block trong RAW output LLM để đo hiệu quả A1 (root cause #1).
# parsed.text đã bị parser strip mood block, không phản ánh LLM có tự report hay không.
_RAW_MOOD_BLOCK_RE = re.compile(
    r"\[\s*(?:vui|bu[ồo]n|b[ựu]c|b[ồo]n[ _]ch[ồo]n|ng[ưu][ợơo]ng|neutral)\s*:\s*-?\d+",
    re.IGNORECASE,
)


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
        turn_logger: TurnLogger | None = None,   # B0: turns.jsonl sink
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
        # A1: _drift + last_drift_report ĐÃ BỎ (Kênh B tắt)
        self._turn_logger = turn_logger
        self._turn_seq = 0
        self.last_filter_verdict: FilterVerdict | None = None
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
        turn_logger: TurnLogger | None = None,
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
            turn_logger=turn_logger,
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
        history_user_text: str | None = None,
        commit_history: bool = True,
        stage_direction: str | None = None,
    ) -> tuple[ParsedResponse, int]:
        """Trả (parsed, level_used). level_used=0 primary, 1 canned.

        `user_text`: đưa vào PROMPT cho LLM (có thể là marker "[Mấy người hỏi...]").
        TASK 5: `history_user_text` — text CHAT GỐC dùng để commit_turn + memory
        (tránh nhiễm history/memory bằng chuỗi ngoặc prompt). None → dùng user_text.
        `commit_history=False` → KHÔNG commit history + KHÔNG extract memory
        (SUMMARY/VIBE không có tin cụ thể).
        """
        request = self._build_request_maybe_with_mood(
            request_id, user_text, event_category, stage_direction,
        )
        hist_text = history_user_text if history_user_text is not None else user_text

        t0 = time.perf_counter()
        result = await self._fb.execute(_CHAIN_ID, request)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        parsed: ParsedResponse = result.value
        if commit_history:
            self._pm.commit_turn(hist_text, parsed.text)
        # A1: canned mood update chỉ khi có tín hiệu mood (defensive).
        if parsed.ok and parsed.mood.dominant() != "neutral":
            self._canned.update_mood(parsed.mood)
        self._record_metrics(parsed, result.level_used)

        # A1: chỉ clear tone flags (Kênh B + drift ĐÃ BỎ)
        self._apply_emotion_feedback(parsed, None)

        # Phase 7.F: auto-extract memory từ turn (fire-and-forget) — dùng text gốc,
        # skip khi commit_history=False (không có tin cụ thể để nhớ).
        if commit_history:
            self._schedule_memory_write(hist_text, parsed, viewer_id, session_id, trigger_type)

        # B0: baseline transcript sink
        self._log_turn(
            kind="chat_reply",
            user_text=user_text,
            parsed=parsed,
            trigger_type=trigger_type,
            level_used=result.level_used,
            latency_ms=latency_ms,
            viewer_id=viewer_id,
            session_id=session_id,
        )
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
        t0 = time.perf_counter()
        result = await self._fb.execute(_CHAIN_ID, request)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        parsed: ParsedResponse = result.value
        # A1: canned mood update chỉ khi parsed.mood có tín hiệu (defensive — LLM
        # cũ vẫn có thể sinh block). Không còn required path.
        if parsed.ok and parsed.mood.dominant() != "neutral":
            self._canned.update_mood(parsed.mood)
        self._record_metrics(parsed, result.level_used)
        # A1: Kênh B bỏ. Chỉ clear tone flags (Prompt đã đọc 1 lần/turn).
        if self._emotion is not None:
            try:
                self._emotion.clear_tone_flags()
            except Exception:
                pass
        # B0: ambient transcript sink (kind=ambient để eval tách chat_reply vs Mai tự nói)
        self._log_turn(
            kind="ambient",
            user_text=None,
            parsed=parsed,
            trigger_type=None,
            level_used=result.level_used,
            latency_ms=latency_ms,
            viewer_id=None,
            session_id=None,
        )
        return parsed

    def commit_self_talk(self, text: str) -> None:
        """A6: đẩy self-talk (ambient) vào history để lượt chat sau khớp continuity.
        Delegate PromptManager. Caller gọi sau khi chốt text cuối (sau dedup regen)."""
        try:
            self._pm.commit_self_talk(text)
        except Exception as e:
            get_logger("llm_turn").warning("commit_self_talk_failed", error=str(e))

    def _build_request_maybe_with_mood(
        self, request_id: str, user_text: str, event_category: str | None,
        stage_direction: str | None = None,
    ):
        if self._emotion is None:
            # Không emotion nhưng vẫn cần chỉ thị sân khấu → dùng build_request_with_mood
            # với mood neutral nếu có stage_direction; ngược lại build_request thường.
            if stage_direction is None:
                return self._pm.build_request(request_id, user_text)
            from interfaces.animation import MoodState
            return self._pm.build_request_with_mood(
                request_id=request_id, user_text=user_text,
                current_mood=MoodState(), stage_direction=stage_direction,
            )
        cause = None
        try:
            cause = self._emotion.active_cause()   # A4
        except Exception:
            pass
        return self._pm.build_request_with_mood(
            request_id=request_id,
            user_text=user_text,
            current_mood=self._emotion.current_mood(),
            event_category=event_category,
            tone_flags=self._emotion.active_tone_flags(),
            cause=cause,
            stage_direction=stage_direction,
        )

    def _apply_emotion_feedback(self, parsed: ParsedResponse, engine_mood_pre) -> None:
        """A1: chỉ còn clear_tone_flags. Kênh B (apply_llm_hint) + drift detect ĐÃ BỎ.

        Mood engine giờ chỉ đi 1 chiều: appraisal event (Kênh A) → engine → prompt.
        LLM không còn tự report mood → không có gì để nudge ngược.
        """
        if self._emotion is None:
            return
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

    def _log_turn(
        self,
        *,
        kind: str,
        user_text: str | None,
        parsed: ParsedResponse,
        trigger_type: str | None,
        level_used: int,
        latency_ms: int | None,
        viewer_id: str | None,
        session_id: str | None,
    ) -> None:
        """Ghi 1 record turn vào turns.jsonl (B0 baseline sink).

        Fail-safe: sink lỗi chỉ log warning, KHÔNG raise (không giết turn).
        """
        if self._turn_logger is None:
            return
        self._turn_seq += 1
        dominant_name, dominant_val = _dominant_mood(parsed)
        raw_text = getattr(parsed, "raw", "") or ""
        record = {
            "turn_id": self._turn_seq,
            "kind": kind,
            "user_text": user_text,
            "mai_text": parsed.text,
            # A1.1: True khi LLM tự sinh mood block trong raw (kể cả parser đã strip
            # ra khỏi mai_text). Đây là số đo hiệu quả A1 — target 0 sau A1.
            "raw_had_mood_block": bool(_RAW_MOOD_BLOCK_RE.search(raw_text)),
            "parse_ok": parsed.ok,
            "mood_dominant": dominant_name,
            "mood_intensity": dominant_val,
            "trigger_type": trigger_type,
            "level_used": level_used,
            "latency_ms": latency_ms,
            "viewer_id": viewer_id,
            "session_id": session_id,
        }
        try:
            self._turn_logger.log_turn(record)
        except Exception as e:  # pragma: no cover — fail-safe
            get_logger("llm_turn").warning("turn_log_failed", error=str(e))

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
