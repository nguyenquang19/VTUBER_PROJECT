_HIGH = ("?", "help", "cứu", "giúp")
def tier1_score(content: str) -> float:
    c = content.lower().strip()
    s = 0.1
    if any(k in c for k in _HIGH): s += 0.4
    if len(c) > 80: s += 0.2
    return round(min(s, 1.0), 2)