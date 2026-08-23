"""Bounded, delivery-aware style guard for public Director speech."""
from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass


_LEADING_PUNCTUATION_RE = re.compile(r"^[\s\"'“”‘’.,!?…:;()\[\]{}-]+", re.UNICODE)
_TRAILING_PUNCTUATION_RE = re.compile(r"[\s\"'“”‘’.,!?…:;()\[\]{}-]+$", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+", re.UNICODE)


def _normalise(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.casefold().strip())


def _normalise_leading(value: str) -> str:
    return _LEADING_PUNCTUATION_RE.sub("", _normalise(value))


def _starts_with_phrase(text: str, phrase: str) -> bool:
    return text == phrase or text.startswith(phrase + " ") or text.startswith(phrase + ",")


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(
        rf"(?<!\w){re.escape(phrase)}(?!\w)", _normalise(text), re.UNICODE,
    ) is not None


def _contains_language_fragment(text: str, fragment: str) -> bool:
    # Configured non-Latin script contamination may be glued to a Vietnamese token.
    if any(ord(char) > 0x024F for char in fragment):
        return _normalise(fragment) in _normalise(text)
    return _contains_phrase(text, fragment)


def looks_like_question(text: str, endings: tuple[str, ...]) -> bool:
    """Recognise explicit or Vietnamese semantic question endings."""
    folded = _normalise(text)
    if not folded:
        return False
    if "?" in folded:
        return True
    tail = _TRAILING_PUNCTUATION_RE.sub("", folded)
    return any(
        tail == ending or tail.endswith(" " + ending)
        for ending in endings
    )


def _sentences(text: str) -> list[str]:
    compact = _WHITESPACE_RE.sub(" ", text.strip())
    return [part.strip() for part in _SENTENCE_BOUNDARY_RE.split(compact) if part.strip()]


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


@dataclass(frozen=True, slots=True)
class SpeechStyleAssessment:
    reasons: tuple[str, ...]
    opener: str | None
    question_like: bool
    phrase: str | None = None
    language_fragment: str | None = None
    grounding_pattern: str | None = None
    malformed_token: str | None = None
    semantic_inference_pattern: str | None = None

    @property
    def valid(self) -> bool:
        return not self.reasons


@dataclass(frozen=True, slots=True)
class SpeechStyleSummary:
    total: int
    formula_openers: int
    questions: int

    @property
    def formula_opener_ratio(self) -> float:
        return self.formula_openers / self.total if self.total else 0.0

    @property
    def question_ratio(self) -> float:
        return self.questions / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class _StyleRecord:
    opener: str | None
    question_like: bool
    phrases: tuple[str, ...] = ()


class SpeechStyleGuard:
    """Track recent delivered style and reject only over-budget candidates."""

    def __init__(
        self,
        *,
        recent_window: int,
        formula_openers: tuple[str, ...],
        max_formula_openers: int,
        max_same_opener: int,
        formula_phrases: tuple[str, ...] = (),
        language_integrity_fragments: tuple[str, ...] = (),
        malformed_token_fragments: tuple[str, ...] = (),
        malformed_token_allowlist: tuple[str, ...] = (),
        malformed_mixed_case_min_prefix_chars: int = 0,
        vague_input_max_words: int = 1,
        vague_grounding_forbidden_patterns: tuple[str, ...] = (),
        semantic_over_inference_patterns: tuple[str, ...] = (),
        max_questions: int,
        question_endings: tuple[str, ...],
        max_sentences: int = 2,
        max_words: int = 32,
    ) -> None:
        self._window = max(1, int(recent_window))
        self._formula_openers = tuple(
            sorted(
                {_normalise(item) for item in formula_openers if _normalise(item)},
                key=len,
                reverse=True,
            )
        )
        self._max_formula_openers = max(0, int(max_formula_openers))
        self._max_same_opener = max(0, int(max_same_opener))
        self._formula_phrases = tuple(
            sorted(
                {_normalise(item) for item in formula_phrases if _normalise(item)},
                key=len,
                reverse=True,
            )
        )
        self._language_integrity_fragments = tuple(
            sorted(
                {
                    _normalise(item)
                    for item in language_integrity_fragments
                    if _normalise(item)
                },
                key=len,
                reverse=True,
            )
        )
        self._malformed_token_fragments = tuple(
            sorted(
                {
                    _normalise(item)
                    for item in malformed_token_fragments
                    if _normalise(item)
                },
                key=len,
                reverse=True,
            )
        )
        self._malformed_token_allowlist = frozenset(
            _normalise(item) for item in malformed_token_allowlist if _normalise(item)
        )
        self._malformed_mixed_case_min_prefix_chars = max(
            0, int(malformed_mixed_case_min_prefix_chars),
        )
        self._vague_input_max_words = max(0, int(vague_input_max_words))
        self._vague_grounding_forbidden_patterns = tuple(
            sorted(
                {
                    _normalise(item)
                    for item in vague_grounding_forbidden_patterns
                    if _normalise(item)
                },
                key=len,
                reverse=True,
            )
        )
        self._semantic_over_inference_patterns = tuple(
            sorted(
                {
                    _normalise(item)
                    for item in semantic_over_inference_patterns
                    if _normalise(item)
                },
                key=len,
                reverse=True,
            )
        )
        self._max_questions = max(0, int(max_questions))
        self._question_endings = tuple(
            _normalise(item) for item in question_endings if _normalise(item)
        )
        self._max_sentences = max(1, int(max_sentences))
        self._max_words = max(1, int(max_words))
        self._recent: deque[_StyleRecord] = deque(maxlen=self._window)

    def opener_for(self, text: str) -> str | None:
        folded = _normalise_leading(text)
        return next(
            (
                opener for opener in self._formula_openers
                if _starts_with_phrase(folded, opener)
            ),
            None,
        )

    def phrases_for(self, text: str) -> tuple[str, ...]:
        return tuple(
            phrase for phrase in self._formula_phrases
            if _contains_phrase(text, phrase)
        )

    def language_fragment_for(self, text: str) -> str | None:
        return next(
            (
                fragment for fragment in self._language_integrity_fragments
                if _contains_language_fragment(text, fragment)
            ),
            None,
        )

    def malformed_token_for(
        self, text: str, *, grounding_text: str | None = None,
    ) -> str | None:
        source = grounding_text or ""
        fragment = next(
            (
                item for item in self._malformed_token_fragments
                if _contains_language_fragment(text, item)
                and not _contains_language_fragment(source, item)
            ),
            None,
        )
        if fragment is not None:
            return fragment
        minimum = self._malformed_mixed_case_min_prefix_chars
        if minimum <= 0:
            return None
        source_tokens = {_normalise(token) for token in _WORD_RE.findall(source)}
        for token in _WORD_RE.findall(text):
            folded = _normalise(token)
            if folded in self._malformed_token_allowlist or folded in source_tokens:
                continue
            if any(
                index >= minimum
                and char.isupper()
                and any(prefix.islower() for prefix in token[:index])
                for index, char in enumerate(token)
            ):
                return token
        return None

    def semantic_inference_pattern_for(
        self, text: str, *, grounding_text: str | None,
    ) -> str | None:
        if grounding_text is None:
            return None
        return next(
            (
                pattern for pattern in self._semantic_over_inference_patterns
                if _contains_phrase(text, pattern)
                and not _contains_phrase(grounding_text, pattern)
            ),
            None,
        )

    def assess(
        self,
        text: str,
        *,
        question_budget_exempt: bool = False,
        grounding_text: str | None = None,
        enforce_semantic_grounding: bool = False,
    ) -> SpeechStyleAssessment:
        opener = self.opener_for(text)
        phrases = self.phrases_for(text)
        language_fragment = self.language_fragment_for(text)
        malformed_token = self.malformed_token_for(
            text, grounding_text=grounding_text,
        )
        question_like = looks_like_question(text, self._question_endings)
        opener_counts = Counter(
            item.opener for item in self._recent if item.opener is not None
        )
        formula_count = sum(opener_counts.values())
        question_count = sum(item.question_like for item in self._recent)
        reasons: list[str] = []
        grounding_pattern = None
        if (
            grounding_text is not None
            and _word_count(grounding_text) <= self._vague_input_max_words
        ):
            grounding_pattern = next(
                (
                    pattern
                    for pattern in self._vague_grounding_forbidden_patterns
                    if _contains_phrase(text, pattern)
                    and not _contains_phrase(grounding_text, pattern)
                ),
                None,
            )
            if grounding_pattern is not None:
                reasons.append("vague_grounding")
        semantic_inference_pattern = None
        if enforce_semantic_grounding:
            semantic_inference_pattern = self.semantic_inference_pattern_for(
                text, grounding_text=grounding_text,
            )
            if semantic_inference_pattern is not None:
                reasons.append("semantic_over_inference")
        if opener is not None and formula_count >= self._max_formula_openers:
            reasons.append("formula_opener_budget")
        if opener is not None and opener_counts[opener] >= self._max_same_opener:
            reasons.append("same_opener_budget")
        if language_fragment is not None:
            reasons.append("language_integrity")
        if malformed_token is not None:
            reasons.append("malformed_token")
        if (
            question_like
            and not question_budget_exempt
            and question_count >= self._max_questions
        ):
            reasons.append("question_budget")
        if len(_sentences(text)) > self._max_sentences:
            reasons.append("sentence_budget")
        if _word_count(text) > self._max_words:
            reasons.append("word_budget")
        return SpeechStyleAssessment(
            reasons=tuple(reasons),
            opener=opener,
            question_like=question_like,
            phrase=phrases[0] if phrases else None,
            language_fragment=language_fragment,
            grounding_pattern=grounding_pattern,
            malformed_token=malformed_token,
            semantic_inference_pattern=semantic_inference_pattern,
        )

    def record(self, text: str) -> tuple[str, ...]:
        if not text or not text.strip():
            return ()
        phrases = self.phrases_for(text)
        self._recent.append(_StyleRecord(
            opener=self.opener_for(text),
            question_like=looks_like_question(text, self._question_endings),
            phrases=phrases,
        ))
        return phrases

    def constraints(self, *, question_budget_exempt: bool = False) -> tuple[tuple[str, ...], bool]:
        opener_counts = Counter(
            item.opener for item in self._recent if item.opener is not None
        )
        formula_count = sum(opener_counts.values())
        if formula_count >= self._max_formula_openers:
            forbidden = self._formula_openers
        else:
            forbidden = tuple(
                opener for opener, count in opener_counts.items()
                if count >= self._max_same_opener
            )
        question_count = sum(item.question_like for item in self._recent)
        avoid_question = (
            not question_budget_exempt and question_count >= self._max_questions
        )
        return forbidden, avoid_question

    @property
    def requires_language_integrity(self) -> bool:
        return bool(
            self._language_integrity_fragments
            or self._malformed_token_fragments
            or self._malformed_mixed_case_min_prefix_chars
        )

    def recent_count(self) -> int:
        return len(self._recent)

    @property
    def max_sentences(self) -> int:
        return self._max_sentences

    @property
    def max_words(self) -> int:
        return self._max_words

    def clamp_shape(self, text: str) -> str:
        """Keep the earliest complete sentence-shaped units within both bounds."""
        selected: list[str] = []
        words = 0
        for sentence in _sentences(text)[: self._max_sentences]:
            sentence_words = _word_count(sentence)
            if selected and words + sentence_words > self._max_words:
                break
            if not selected and sentence_words > self._max_words:
                tokens = sentence.split()[: self._max_words]
                return " ".join(tokens).rstrip(",;:") + "."
            selected.append(sentence)
            words += sentence_words
        return " ".join(selected).strip() or text.strip()

    def clamp_questions(self, text: str) -> str:
        """Drop question sentences only when a grounded statement remains."""
        statements = [
            sentence for sentence in _sentences(text)
            if not looks_like_question(sentence, self._question_endings)
        ]
        if not statements:
            return text
        return " ".join(statements).strip()

    def snapshot(self) -> tuple[tuple[str | None, bool], ...]:
        return tuple((item.opener, item.question_like) for item in self._recent)


def summarize_speech_style(
    texts: list[str],
    *,
    formula_openers: tuple[str, ...],
    question_endings: tuple[str, ...],
) -> SpeechStyleSummary:
    """Compute full-delivery style aggregates with production normalisation."""
    guard = SpeechStyleGuard(
        recent_window=1,
        formula_openers=formula_openers,
        max_formula_openers=1,
        max_same_opener=1,
        max_questions=1,
        question_endings=question_endings,
        max_sentences=1_000_000,
        max_words=1_000_000,
    )
    compact = [text for text in texts if text and text.strip()]
    return SpeechStyleSummary(
        total=len(compact),
        formula_openers=sum(guard.opener_for(text) is not None for text in compact),
        questions=sum(looks_like_question(text, question_endings) for text in compact),
    )
