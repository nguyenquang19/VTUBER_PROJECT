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
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from interfaces.filter import FilterVerdict
from interfaces.llm import LLMService
from orchestrator.fallback_manager import FallbackManager
from orchestrator.logger import TurnLogger, get_logger
from services.llm.canned_response import CannedResponder
from services.llm.parser import ParsedResponse, parse_response
from services.llm.prompt_manager import PromptManager
from services.agent.types import (
    AgentEventKind,
    AgentEventSource,
    EventProvenance,
    GroundedEvent,
)

_CHAIN_ID = "llm"
TokenSink = Callable[[str], None]

# A1.1: detect mood block trong RAW output LLM để đo hiệu quả A1 (root cause #1).
# parsed.text đã bị parser strip mood block, không phản ánh LLM có tự report hay không.
_RAW_MOOD_BLOCK_RE = re.compile(
    r"\[\s*(?:vui|bu[ồo]n|b[ựu]c|b[ồo]n[ _]ch[ồo]n|ng[ưu][ợơo]ng|neutral)\s*:\s*-?\d+",
    re.IGNORECASE,
)

# Phase 8 data pipeline: schema version cho turns.jsonl (versioned để export parse đúng).
_TURN_SCHEMA_VERSION = 2
_MOOD_DIMS = ("vui", "buon", "buc", "bon_chon", "nguong")


def _context_block_of(request: Any) -> str | None:
    """Rút system message '[Context...]' đã render (mood directive + cause + stage)."""
    try:
        for m in getattr(request, "messages", []):
            content = getattr(m, "content", "")
            if getattr(m, "role", "") == "system" and content.startswith("[Context"):
                return content
    except Exception:
        pass
    return None


def _mood_dict(mood: Any) -> dict | None:
    try:
        return {d: int(getattr(mood, d, 0)) for d in _MOOD_DIMS}
    except Exception:
        return None


def _cause_dict(cause: Any) -> dict | None:
    if cause is None:
        return None
    try:
        return {"alias": cause.viewer_alias, "intent": cause.intent_short}
    except Exception:
        return None


def _verdict_dict(verdict: Any, was_regen: bool) -> dict | None:
    if verdict is None:
        return None
    try:
        cats = [getattr(c, "value", str(c)) for c in getattr(verdict, "categories_hit", [])]
        return {"passed": bool(getattr(verdict, "passed", True)), "categories": cats, "regen": was_regen}
    except Exception:
        return None


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
        pref_logger: Any = None,        # T2: pref_pairs.jsonl (JsonlWriter | None)
        session_id: str | None = None,
        agent_state: Any = None,
        agent_context_renderer: Any = None,
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
        self._pref_logger = pref_logger
        self.session_id = session_id or str(uuid.uuid4())
        self._agent_state = agent_state
        self._agent_context_renderer = agent_context_renderer
        self._turn_seq = 0
        self.last_turn_id = 0          # T3: turn cuối để dashboard rating gắn vào
        self._log_cause: Any = None    # T1: cause snapshot trước khi clear
        self.last_filter_verdict: FilterVerdict | None = None
        self._last_filter_initial_verdict: FilterVerdict | None = None
        self._last_was_regen = False
        self._last_rejected_text: str | None = None
        self._memory_writes_scheduled = 0
        self._memory_writes_skipped = 0
        self._fb.register_chain(
            _CHAIN_ID,
            [self._primary, self._canned_handler],
            [timeout_primary_s, timeout_canned_s],
        )

    @property
    def filter_enabled(self) -> bool:
        return self._regen is not None

    def set_regenerator(self, regenerator: Any = None) -> None:
        """Enable or disable output filtering for subsequent turns."""
        self._regen = regenerator

    @property
    def agent_context_enabled(self) -> bool:
        return self._agent_context_renderer is not None

    def set_agent_context_renderer(self, renderer: Any = None) -> None:
        self._agent_context_renderer = renderer

    def _reset_filter_tracking(self) -> None:
        self.last_filter_verdict = None
        self._last_filter_initial_verdict = None
        self._last_was_regen = False
        self._last_rejected_text = None

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
        pref_logger: Any = None,
        session_id: str | None = None,
        agent_state: Any = None,
        agent_context_renderer: Any = None,
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
            pref_logger=pref_logger,
            session_id=session_id,
            agent_state=agent_state,
            agent_context_renderer=agent_context_renderer,
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
        # T1/T2 data: bắt was_regen + rejected_text (bản đầu bị chặn) để log + DPO pair.
        regenerator = self._regen
        if regenerator is not None:
            pre_text = parsed.text
            parsed, verdict = await regenerator.check_and_maybe_regen(
                request, parsed, on_token=self._on_token
            )
            self.last_filter_verdict = verdict
            self._last_filter_initial_verdict = getattr(
                regenerator, "last_initial_verdict", None,
            ) or verdict
            if parsed.text != pre_text:
                self._last_was_regen = True
                self._last_rejected_text = pre_text
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
        self._reset_filter_tracking()
        effective_session_id = session_id or self.session_id
        grounded_context = self._render_agent_context(user_text)
        request = self._build_request_maybe_with_mood(
            request_id, user_text, event_category, stage_direction, grounded_context,
        )
        hist_text = history_user_text if history_user_text is not None else user_text
        # T1: snapshot cause + history_len TRƯỚC khi clear/commit (clear_tone_flags
        # ở _apply_emotion_feedback xoá cause; commit làm history_len đổi).
        try:
            self._log_cause = self._emotion.active_cause() if self._emotion else None
        except Exception:
            self._log_cause = None
        history_len_at_gen = len(self._pm.history())

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
            self._schedule_memory_write(
                hist_text, parsed, viewer_id, effective_session_id, trigger_type,
            )

        # B0 + T1: transcript sink làm giàu (SFT record)
        log_kind = "director_read" if trigger_type == "director_read" else "chat_reply"
        self._log_turn(
            kind=log_kind,
            user_text=user_text,
            parsed=parsed,
            trigger_type=trigger_type,
            level_used=result.level_used,
            latency_ms=latency_ms,
            viewer_id=viewer_id,
            session_id=effective_session_id,
            extra=self._build_log_extra(request, event_category, history_len_at_gen),
        )
        # T2: filter regen → cặp DPO (rejected = bản bị chặn, chosen = bản pass)
        if self._last_was_regen and self._last_rejected_text:
            reason_verdict = self._last_filter_initial_verdict or self.last_filter_verdict
            cats = [getattr(c, "value", str(c))
                    for c in getattr(reason_verdict, "categories_hit", [])]
            reason = f"filter:{cats[0]}" if cats else "filter:regen"
            self.log_pref_pair(self._last_rejected_text, parsed.text, reason,
                               session_id=effective_session_id,
                               user_text=user_text, request=request)
        self._record_speech_event(
            request_id=request_id,
            parsed=parsed,
            session_id=effective_session_id,
            mode="chat",
            trigger_type=trigger_type,
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
        self._reset_filter_tracking()
        request = self._pm.build_request(
            request_id, prompt_text,
            grounded_context=self._render_agent_context(prompt_text),
        )
        # T1: ambient — cause snapshot + history_len trước clear
        try:
            self._log_cause = self._emotion.active_cause() if self._emotion else None
        except Exception:
            self._log_cause = None
        history_len_at_gen = len(self._pm.history())
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
        # B0 + T1: ambient transcript sink (kind=ambient tách với chat_reply)
        self._log_turn(
            kind="ambient",
            user_text=None,
            parsed=parsed,
            trigger_type=None,
            level_used=result.level_used,
            latency_ms=latency_ms,
            viewer_id=None,
            session_id=self.session_id,
            extra=self._build_log_extra(request, None, history_len_at_gen),
        )
        self._record_speech_event(
            request_id=request_id,
            parsed=parsed,
            session_id=self.session_id,
            mode="ambient",
            trigger_type=None,
        )
        return parsed

    def _record_speech_event(
        self,
        *,
        request_id: str,
        parsed: ParsedResponse,
        session_id: str,
        mode: str,
        trigger_type: str | None,
    ) -> None:
        if self._agent_state is None or not parsed.text:
            return
        try:
            self._agent_state.record(GroundedEvent(
                event_id=f"agent:speech:{session_id}:{request_id}",
                kind=AgentEventKind.SPEECH_FINAL,
                source=AgentEventSource.LLM,
                timestamp=datetime.now(timezone.utc),
                confidence=1.0 if parsed.ok else 0.7,
                payload={
                    "text": parsed.text,
                    "mode": mode,
                    "trigger_type": trigger_type,
                    "output_ok": parsed.ok,
                },
                provenance=EventProvenance(
                    producer="llm_turn_runner",
                    source_event_id=request_id,
                    session_id=session_id,
                ),
            ))
        except Exception as exc:
            get_logger("llm_turn").warning("speech_agent_event_failed", error=str(exc))

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
        grounded_context: str | None = None,
    ):
        if self._emotion is None:
            # Không emotion nhưng vẫn cần chỉ thị sân khấu → dùng build_request_with_mood
            # với mood neutral nếu có stage_direction; ngược lại build_request thường.
            if stage_direction is None:
                return self._pm.build_request(
                    request_id, user_text, grounded_context=grounded_context,
                )
            from interfaces.animation import MoodState
            return self._pm.build_request_with_mood(
                request_id=request_id, user_text=user_text,
                current_mood=MoodState(), stage_direction=stage_direction,
                grounded_context=grounded_context,
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
            grounded_context=grounded_context,
        )

    def _render_agent_context(self, query: str) -> str | None:
        if self._agent_state is None or self._agent_context_renderer is None:
            return None
        try:
            return self._agent_context_renderer.render(self._agent_state.snapshot(), query)
        except Exception as exc:
            get_logger("llm_turn").warning("agent_context_render_failed", error=str(exc))
            return None

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
        extra: dict | None = None,
    ) -> None:
        """Ghi 1 record turn vào turns.jsonl (B0 baseline + Phase 8 data pipeline).

        Fail-safe: sink lỗi chỉ log warning, KHÔNG raise (không giết turn). Mọi field
        làm giàu (T1) lấy best-effort — lỗi lấy field nào → null, không giết log.
        """
        self._turn_seq += 1
        self.last_turn_id = self._turn_seq   # T3: dashboard rating gắn turn cuối
        if self._turn_logger is None:
            return
        dominant_name, dominant_val = _dominant_mood(parsed)
        raw_text = getattr(parsed, "raw", "") or ""
        # T4: sanitize PII — hash viewer_id (không lưu channel id gốc), mask
        # email/phone/token trong user_text.
        from services.data.sanitize import hash_viewer_id, mask_known_identifier, mask_pii
        record = {
            "schema_version": _TURN_SCHEMA_VERSION,
            "turn_id": self._turn_seq,
            "kind": kind,
            "user_text": mask_pii(user_text),
            "mai_text": mask_pii(parsed.text),
            # A1.1: True khi LLM tự sinh mood block trong raw (kể cả parser đã strip
            # ra khỏi mai_text). Đây là số đo hiệu quả A1 — target 0 sau A1.
            "raw_had_mood_block": bool(_RAW_MOOD_BLOCK_RE.search(raw_text)),
            "parse_ok": parsed.ok,
            "mood_dominant": dominant_name,
            "mood_intensity": dominant_val,
            "trigger_type": trigger_type,
            "level_used": level_used,
            "latency_ms": latency_ms,
            "viewer_id": hash_viewer_id(viewer_id),   # T4: hash, KHÔNG lưu id gốc
            "session_id": session_id,
            "source": trigger_type or kind,
        }
        if extra:
            if extra.get("context_block"):
                extra["context_block"] = mask_pii(extra["context_block"])
            cause = extra.get("mood_cause")
            if isinstance(cause, dict) and cause.get("alias"):
                alias = str(cause["alias"])
                for key in ("user_text", "mai_text"):
                    record[key] = mask_known_identifier(record.get(key), alias)
                extra["context_block"] = mask_known_identifier(
                    extra.get("context_block"), alias,
                )
                cause["alias"] = "[PII]"
            record.update(extra)
        try:
            self._turn_logger.log_turn(record)
        except Exception as e:  # pragma: no cover — fail-safe
            get_logger("llm_turn").warning("turn_log_failed", error=str(e))

    def log_pref_pair(
        self, rejected: str, chosen: str, reason: str,
        session_id: str | None = None, user_text: str | None = None,
        request: Any = None,
    ) -> None:
        """T2: ghi 1 cặp DPO (chosen > rejected) vào pref_pairs.jsonl. Fail-safe.

        Nguồn: filter regen (auto trong run_turn) + dedup ambient (caller gọi).
        Cùng prompt, chosen ≠ rejected → data DPO chuẩn, không cần nhãn tay."""
        if self._pref_logger is None or not rejected or not chosen or rejected == chosen:
            return
        try:
            from services.data.sanitize import mask_known_identifier, mask_pii
            persona_v = None
            cause_alias = getattr(self._log_cause, "viewer_alias", None)
            try:
                persona_v = self._pm.version
            except Exception:
                pass
            record = {
                "schema_version": 1,
                "turn_id": self._turn_seq,
                "session_id": session_id or self.session_id,
                "prompt_ref": {
                    "persona_version": persona_v,
                    "context_block": mask_known_identifier(
                        mask_pii(_context_block_of(request)), cause_alias,
                    ),
                    "user_text": mask_known_identifier(mask_pii(user_text), cause_alias),
                },
                "rejected": mask_known_identifier(mask_pii(rejected), cause_alias),
                "chosen": mask_known_identifier(mask_pii(chosen), cause_alias),
                "reason": reason,
                "source": reason.split(":", 1)[0],
            }
            self._pref_logger.write(record)
        except Exception as e:  # pragma: no cover — fail-safe
            get_logger("llm_turn").warning("pref_pair_log_failed", error=str(e))

    def _build_log_extra(self, request: Any, event_category: str | None,
                         history_len: int) -> dict:
        """T1: gom field làm giàu SFT record (best-effort, không raise)."""
        extra: dict = {"persona_version": None, "context_block": None,
                       "mood_state": None, "mood_cause": None,
                       "event_category": event_category, "history_len": history_len,
                       "was_regen": bool(self._last_was_regen), "filter_verdict": None}
        try:
            extra["persona_version"] = self._pm.version
        except Exception:
            pass
        extra["context_block"] = _context_block_of(request)
        if self._emotion is not None:
            try:
                extra["mood_state"] = _mood_dict(self._emotion.current_mood())
            except Exception:
                pass
            try:
                extra["mood_cause"] = _cause_dict(self._log_cause)
            except Exception:
                pass
        extra["filter_verdict"] = _verdict_dict(self.last_filter_verdict, self._last_was_regen)
        return extra

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
            v = self._last_filter_initial_verdict or self.last_filter_verdict
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
