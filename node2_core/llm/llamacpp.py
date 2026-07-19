import json, requests
from typing import Iterator
from ..config import LLM_HOST, LLM_TIMEOUT_S, LLM_N_PREDICT

# Raw /completion, stream=true. Hủy = đóng response (llama.cpp dừng slot).
class LlamaCppClient:
    def __init__(self, host=LLM_HOST, timeout=LLM_TIMEOUT_S, n_predict=LLM_N_PREDICT):
        self._host, self._timeout, self._n = host, timeout, n_predict

    def stream(self, prompt: str, cancel_check) -> Iterator[str]:
        payload = {"prompt": prompt, "n_predict": self._n, "stream": True}
        r = requests.post(f"{self._host}/completion", json=payload,
                          stream=True, timeout=self._timeout)
        try:
            for line in r.iter_lines(decode_unicode=True):
                if cancel_check():          # có tín hiệu dừng -> cắt sớm
                    break
                if not line or not line.startswith("data: "):
                    continue
                chunk = json.loads(line[6:])
                if chunk.get("content"):
                    yield chunk["content"]
                if chunk.get("stop"):
                    break
        finally:
            r.close()     