from typing import Protocol

class CrisisDetector(Protocol):
    def is_crisis(self, content: str) -> bool: ...

class NoopCrisisDetector:
    def is_crisis(self, content: str) -> bool: return False