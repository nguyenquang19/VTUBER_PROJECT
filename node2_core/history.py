from collections import defaultdict, deque
from .config import CTX_BUDGET_CHARS

class RawHistory:
    def __init__(self, budget=CTX_BUDGET_CHARS):
        self._h = defaultdict(lambda: deque()); self._budget = budget
    def context(self, session: str) -> str:
        return "\n".join(self._h[session])
    def append(self, session: str, line: str):
        d = self._h[session]; d.append(line)
        while sum(len(x) for x in d) > self._budget and len(d) > 1:
            d.popleft()