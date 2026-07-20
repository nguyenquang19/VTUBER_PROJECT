import json, requests
from typing import Iterator
from ..config import LLM_HOST, LLM_TIMEOUT_S, LLM_N_PREDICT

class LlamaCppClient:
    def __init__(self, host=LLM_HOST, timeout=LLM_TIMEOUT_S, n_predict=LLM_N_PREDICT):
        self._host, self._timeout, self._n = host, timeout, n_predict

    def stream(self, messages: list[dict], cancel_check) -> Iterator[str]:
        payload = {"messages": messages, "max_tokens": self._n, "stream": True,
            "chat_template_kwargs": {"enable_thinking": False}}
        r = requests.post(f"{self._host}/v1/chat/completions", json=payload,
                          stream=True, timeout=self._timeout)
        try:
            # decode_unicode=False -> nhận BYTES, tự decode utf-8 (không để requests đoán latin-1)
            for raw in r.iter_lines(decode_unicode=False):
                if cancel_check(): break
                if not raw: continue
                line = raw.decode("utf-8")          # ÉP UTF-8 tường minh
                if not line.startswith("data: "): continue
                data = line[6:]
                if data.strip() == "[DONE]": break
                delta = json.loads(data)["choices"][0]["delta"]
                if delta.get("content"): yield delta["content"]
        finally:
            r.close()