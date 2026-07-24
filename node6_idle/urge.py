import random

from . import config as C

# Mức muốn nói (he-thong-logic §4). MỘT con số, code tính, so với ngưỡng.
# Code KHÔNG quyết "có nói không" bằng model (tốn slot GPU mỗi vài giây) —
# nó chỉ làm phép tính với con số MOOD mà model đã đưa ra. Không luật chặn ngoài.


def compute_urge(facts: dict) -> float:
    """Mức muốn nói từ dữ kiện. Càng cao càng muốn lên tiếng."""
    f = facts or {}

    # Có người đang nói bằng GIỌNG -> về gần 0. Mai chen vào lúc đó là phá (§4).
    if f.get("someone_speaking"):
        return 0.0

    urge = 0.0
    chat_silent = f.get("chat_silent_sec") or 0.0
    urge += (chat_silent / 60.0) * C.URGE_CHAT_SILENT_PER_MIN

    mai_silent = f.get("mai_silent_sec") or 0.0
    urge += (mai_silent / 60.0) * C.URGE_MAI_SILENT_PER_MIN

    # Vừa nói xong -> tụt mạnh (đè phần cộng ở trên).
    if mai_silent < C.URGE_JUST_SPOKE_CUTOFF_SEC:
        urge *= C.URGE_JUST_SPOKE_DAMP

    if f.get("just_joined"):
        urge += C.URGE_JUST_JOINED_BONUS      # người mới vào -> nhảy vọt

    if f.get("pending_topic"):
        urge += C.URGE_PENDING_TOPIC_BONUS    # đang kể dở -> giữ cao, không tụt

    pulse = f.get("room_pulse") or {}
    if pulse.get("msgs_30s", 0) >= C.URGE_ROOM_BUSY_MSGS:
        urge -= C.URGE_ROOM_BUSY_PENALTY      # chat đang sôi -> giảm

    return max(0.0, urge)


def compute_threshold(mood: dict, rng=random) -> float:
    """Ngưỡng để lên tiếng. MOOD điều khiển ngưỡng -> model gián tiếp điều
    khiển nhịp nói của chính nó (§4). Cộng nhiễu ngẫu nhiên cho nhịp tự nhiên."""
    m = mood or {}
    th = C.THRESHOLD_BASE
    th += (m.get("bon_chon", 0) or 0) * C.THRESHOLD_BON_CHON_PER_POINT
    th += (m.get("vui", 0) or 0) * C.THRESHOLD_VUI_PER_POINT
    th += (m.get("buon", 0) or 0) * C.THRESHOLD_BUON_PER_POINT
    th += (m.get("nguong", 0) or 0) * C.THRESHOLD_NGUONG_PER_POINT
    th = max(C.THRESHOLD_MIN, th)
    noise = 1.0 + rng.uniform(-C.THRESHOLD_NOISE_FRAC, C.THRESHOLD_NOISE_FRAC)
    return th * noise


def should_speak(facts: dict, mood: dict, rng=random) -> bool:
    return compute_urge(facts) >= compute_threshold(mood, rng)
