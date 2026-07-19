import os
LLM_HOST = os.getenv("LLM_HOST", "http://localhost:8080")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "20"))   # timeout cứng
LLM_N_PREDICT = int(os.getenv("LLM_N_PREDICT", "256"))
MOD_TIMEOUT_S = float(os.getenv("MOD_TIMEOUT_S", "2"))
INGRESS_PORT  = int(os.getenv("NODE2_PORT", "8802"))
NODE3_HOST    = os.getenv("NODE3_HOST", "127.0.0.1")
NODE3_PORT    = int(os.getenv("NODE3_PORT", "8803"))
NODE3_STOP_PORT = int(os.getenv("NODE3_STOP_PORT", "8804"))
CTX_BUDGET_CHARS = int(os.getenv("CTX_BUDGET_CHARS", "6000"))