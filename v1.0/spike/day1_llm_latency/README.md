# Spike Day 1 — LLM Latency Benchmark

Đo TTFT + decode speed của Gemma 4 12B Q4_K_M trên RTX 5060 Ti 16GB.
Spec: [ARCHITECTURE.md Section 0.2](../../docs/ARCHITECTURE.md).

## Prerequisites

- llama.cpp build với `GGML_CUDA=ON`, `llama-server.exe` chạy được
- Model `gemma-4-12b-Q4_K_M.gguf` ở `models/llm/`
- venv activate (`.\venv\Scripts\Activate.ps1`)
- `nvidia-smi` chạy được (cho scenario 5)

## Chạy

### Terminal 1 — start llama-server

```powershell
# Từ thư mục llama.cpp
.\build\bin\Release\llama-server.exe `
  -m E:\BAI_CUA_DUC\AI_VTUBER\v1.0\models\llm\gemma-4-12b-Q4_K_M.gguf `
  -ngl 999 `
  -c 4096 `
  --cache-type-k q8_0 --cache-type-v q8_0 `
  --port 8080
```

Đợi log "HTTP server listening" mới sang bước tiếp.

### Terminal 2 — chạy benchmark

```powershell
cd E:\BAI_CUA_DUC\AI_VTUBER\v1.0
.\venv\Scripts\Activate.ps1
cd spike\day1_llm_latency
python benchmark.py --endpoint http://localhost:8080
```

**Flow:**
1. S1 Cold — script sẽ pause, yêu cầu restart llama-server rồi Enter
2. S2/S3/S4 — chạy warmup + measure tự động
3. S5 Overheating — chạy 30 phút liên tục, sample GPU temp mỗi 10s

**Tổng thời gian:** ~35-40 phút (chủ yếu là S5).

### Options

```powershell
# Chỉ chạy S1-S4, skip S5 (nhanh hơn, chỉ ~2 phút):
python benchmark.py --scenarios 1,2,3,4

# S5 với duration ngắn hơn (10 phút):
python benchmark.py --scenarios 5 --overheat-sec 600
```

## Output

- `results.json` — số đo chi tiết
- Console in bảng summary + so target
- **Copy `report_template.md` → `spike/day1_report.md`** và điền số + Go/No-go

## No-go criteria (dừng, báo user, KHÔNG tự re-architect)

- TTFT cold > 1000 ms
- Decode < 30 tok/s ở bất kỳ scenario nào
- Overheating throttle_ratio > 30%
