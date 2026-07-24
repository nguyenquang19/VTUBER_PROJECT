import os
LLM_HOST = os.getenv("LLM_HOST", "http://localhost:8080")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "20"))   # timeout cứng
LLM_N_PREDICT = int(os.getenv("LLM_N_PREDICT", "256"))
MOD_TIMEOUT_S = float(os.getenv("MOD_TIMEOUT_S", "2"))
INGRESS_PORT  = int(os.getenv("NODE2_PORT", "8802"))
NODE3_HOST    = os.getenv("NODE3_HOST", "127.0.0.1")
NODE3_PORT    = int(os.getenv("NODE3_PORT", "8803"))
NODE3_STOP_PORT = int(os.getenv("NODE3_STOP_PORT", "8804"))
# Ngân sách ký tự cho TOÀN BỘ prompt (không chỉ lịch sử). Bộ nhân vật ~4500
# ký tự luôn giữ; phần còn lại chia cho mood/hồ sơ/lịch sử. Context llama-server
# đang -c 4096 token (~2.8-3.5 ký tự/token với tiếng Việt) trừ ~200 token output
# -> để 10000 cho an toàn, chỉnh qua env nếu đổi -c.
CTX_BUDGET_CHARS = int(os.getenv("CTX_BUDGET_CHARS", "10000"))
# Lề dự phòng cho các khối cố định ngoài bộ nhân vật (mood + hồ sơ node5 +
# dòng nhắc cuối prompt). Dùng khi chia ngân sách lịch sử cho SessionMemory.
CTX_FIXED_RESERVE_CHARS = int(os.getenv("CTX_FIXED_RESERVE_CHARS", "800"))
CHAT_TTL_SEC = 30    # con số khởi điểm, chỉnh sau khi chạy thật
AUTO_TTL_SEC = 15

SCORE_CRISIS = 1_000_000.0     # cố định, không gì vượt qua
SCORE_CHAT_BASE = 100.0
SCORE_AUTO_BASE = 10.0
SCORE_MENTION_MAI_BONUS = 30.0
SCORE_TIER1_WEIGHT = 20.0             # nhân với tier1_score (0.1-1.0)
SCORE_CHAT_WAIT_PER_SEC = 1.0         # CHAT chờ lâu -> cộng dần
SCORE_AUTO_WAIT_PENALTY_PER_SEC = 0.5 # AUTO chờ lâu -> trừ dần

MOOD_DB_PATH = os.getenv("MOOD_DB_PATH", "data/mood.db")
MOOD_LOG_PATH = os.getenv("MOOD_LOG_PATH", "data/mood-history.jsonl")
