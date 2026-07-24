import re

# "mai" là tên Mai NHƯNG cũng là từ chỉ thời gian rất phổ biến (ngày mai,
# mai mốt...). Substring `"mai" in text` còn dính "email", "mainly".
# -> khớp theo RANH GIỚI TỪ, rồi loại các cụm chỉ thời gian; còn "mai"
# đứng riêng thì coi là gọi tên.
_MAI_WORD = re.compile(r"\bmai\b", re.IGNORECASE | re.UNICODE)
_MAI_TIME = re.compile(
    r"\b(ngày|hôm|sáng|trưa|chiều|tối|đêm)\s+mai\b"
    r"|\bmai\s+(mốt|sau|này|kia)\b",
    re.IGNORECASE | re.UNICODE,
)


def mentions_mai(text: str) -> bool:
    """True nếu tin có vẻ đang GỌI TÊN Mai (không phải 'ngày mai')."""
    t = text or ""
    if not _MAI_WORD.search(t):
        return False
    stripped = _MAI_TIME.sub(" ", t)   # bỏ cụm chỉ thời gian
    return bool(_MAI_WORD.search(stripped))
