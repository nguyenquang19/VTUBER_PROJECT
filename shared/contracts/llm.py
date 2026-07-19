from typing import Protocol, Iterator

class LLMClient(Protocol):
    # Streaming để hủy sớm được. cancel_check() trả True -> dừng generate.
    def stream(self, prompt: str, cancel_check) -> Iterator[str]: ...