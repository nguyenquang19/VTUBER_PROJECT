#!/usr/bin/env bash
# GĐ0: xác nhận llama.cpp server đang chạy và trả lời được.
# Yêu cầu: đã tự chạy `llama-server --model models/xxx.gguf --port 8080` ở terminal khác.
set -e
HOST="${LLM_HOST:-http://localhost:8080}"
curl -s "$HOST/completion" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"ping","n_predict":16,"stream":false}' | head -c 300
echo