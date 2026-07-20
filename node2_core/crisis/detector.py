from .keywords import SELF_HARM, VIOLENCE, PLATFORM_SEVERE, THRESHOLDS, SELF_HARM_PATTERNS
from .review_log import log_review

class RuleBasedCrisisDetector:
    def __init__(self, review=True):
        self._review = review

    def _score(self, content: str):
        low = content.lower()
        groups = {"self_harm": SELF_HARM, "violence": VIOLENCE, "platform": PLATFORM_SEVERE}
        best = ("none", 0.0)
        for name, kws in groups.items():
            hits = sum(1 for k in kws if k in low)
            if hits:
                conf = min(0.3 + 0.35 * hits, 1.0)
                if conf > best[1]: best = (name, conf)
        # Mẫu co-occurrence cho self-harm: bắt biến thể diễn đạt khác chữ.
        # Mỗi mẫu = tập từ phải CÙNG xuất hiện trong câu.
        for pattern in SELF_HARM_PATTERNS:
            if all(tok in low for tok in pattern):
                conf = 0.6   # đủ vượt ngưỡng self_harm (0.30)
                if conf > best[1]: best = ("self_harm", conf)
        return best

    def is_crisis(self, content: str) -> bool:
        group, conf = self._score(content)
        if group == "none": return False
        threshold = THRESHOLDS.get(group, 0.5)
        crisis = conf >= threshold
        if self._review and conf > 0:
            log_review(content, group, conf, threshold, triggered=crisis)
        return crisis