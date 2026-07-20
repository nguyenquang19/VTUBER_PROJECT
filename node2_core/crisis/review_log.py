import json, os
from datetime import datetime, timezone

_PATH = os.getenv("CRISIS_REVIEW_LOG", "data/crisis_review.jsonl")

# Ghi mọi nghi ngờ (kể cả dưới ngưỡng) để người thật xem lại. KHÔNG chặn luồng.
def log_review(content, group, confidence, threshold, triggered):
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "group": group, "confidence": round(confidence, 3),
           "threshold": threshold, "triggered": triggered,
           "content": content}
    try:
        with open(_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass   # log lỗi không được làm sập detection