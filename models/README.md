# Model storage (không commit vào git)

Đặt file `.gguf` ở đây, hoặc trỏ `LLM_MODEL_PATH` tới nơi khác trên máy.

Tải ví dụ:
    huggingface-cli download <repo> <file>.gguf --local-dir ./models

Chạy server:
    llama-server --model ./models/<file>.gguf --port 8080 -c 4096 --parallel 2