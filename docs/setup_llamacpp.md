# Bước A.1 — Dựng llama.cpp chạy Qwen3.5-9B (GGUF) trong WSL2

> **Đã chốt dùng GGUF + llama.cpp thay vì AWQ + vLLM.**
> Lý do: vòng đời train→convert→deploy (Tầng 3, hàng tháng) nhẹ hơn nhiều,
> chạy được trên CPU+RAM thay vì cần 20GB+ VRAM để convert như AWQ.
> Đánh đổi: chậm hơn AWQ một chút trên GPU — chấp nhận được vì LLM 9B
> vẫn đủ nhanh cho mục tiêu <500ms nhờ sentence-streaming.
> (Ghi chú: nếu sau này có thời gian, có thể thử lại AWQ/vLLM để so sánh
> tốc độ thực tế — xem docs/setup_vllm.md đã lưu làm tham khảo.)

> ⚠️ **QUAN TRỌNG — model nền là UNCENSORED (đã gỡ safety filter gốc).**
> Ranh giới an toàn giờ hoàn toàn dựa vào `config/persona_core.yaml`
> (phần `boundaries.hard_rules`) qua system prompt, KHÔNG còn lớp lọc
> gốc của Qwen bảo vệ nữa. Đảm bảo system prompt LUÔN được nạp đầy đủ
> mỗi lần gọi model (Bước A) — đây là hàng rào DUY NHẤT còn lại.

## BƯỚC 0 — Kiểm tra GPU trong WSL2
```bash
nvidia-smi
```
✅ Checkpoint: thấy RTX 5060 Ti, 16GB.

---

## BƯỚC 1 — Build llama.cpp với CUDA
```bash
sudo apt-get update
sudo apt-get install -y pciutils build-essential cmake curl libcurl4-openssl-dev git

git clone https://github.com/ggml-org/llama.cpp
cmake llama.cpp -B llama.cpp/build \
  -DBUILD_SHARED_LIBS=OFF -DGGML_CUDA=ON
cmake --build llama.cpp/build --config Release -j --clean-first \
  --target llama-cli llama-server llama-gguf-split

# copy binary ra thư mục gốc cho tiện gọi
cp llama.cpp/build/bin/llama-* llama.cpp/
```
✅ Checkpoint: `ls llama.cpp/llama-server` tồn tại, không lỗi build.
⚠️ Nếu lỗi thiếu CUDA toolkit: cài `nvidia-cuda-toolkit` hoặc CUDA toolkit for WSL từ NVIDIA.

---

## BƯỚC 2 — Tải model GGUF UNCENSORED, đã tắt thinking (Q4_K_M, ~5.63GB)
```bash
pip install -U huggingface_hub --break-system-packages

huggingface-cli download nandukmelath/Qwen3.5-9B-Uncensored-nothink-GGUF \
  --include "*Q4_K_M*" \
  --local-dir ~/models/qwen3.5-9b-gguf
```
✅ Checkpoint: có file `.gguf` ~5.63GB trong thư mục.

> **Vì sao chọn bản này cụ thể (không phải bản uncensored khác):**
> - Nền tảng: `HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive` (uncensored,
>   dùng làm "phôi sạch" để tự SFT/DPO sau này — đúng chiến lược đã chốt).
> - **Đã patch tắt thinking NGAY Ở TẦNG CHAT TEMPLATE** (`<think></think>` rỗng
>   thay vì sinh 400+ token suy luận) → tránh trễ 15-30 GIÂY mỗi câu trả lời.
>   Đây là khác biệt SỐNG CÒN cho mục tiêu <500ms — không chỉ dựa vào cờ
>   `--reasoning-format` ở server (vẫn giữ cờ đó làm lớp phòng hờ thứ 2).
> - GGUF sẵn, không cần tự convert từ bản HuggingFace gốc.
>
> Nếu muốn chất lượng cao hơn (đổi lấy dung lượng): đổi `Q4_K_M` thành `Q6_K`
> hoặc `Q8_0` trong lệnh `--include` (kiểm tra tên file chính xác trên trang
> HuggingFace của repo trước khi tải).

---

## BƯỚC 3 — Chạy llama-server (tương đương vllm serve)
```bash
./llama.cpp/llama-server \
  -m ~/models/qwen3.5-9b-gguf/Qwen3.5-9B-Uncensored-nothink-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8000 \
  -ngl 99 \
  -c 16384 \
  -fa on \
  --jinja \
  --reasoning-format none \
  --temp 0.85
```
> Tên file chính xác có thể khác chút — kiểm tra bằng `ls ~/models/qwen3.5-9b-gguf/`
> sau khi tải xong ở Bước 2, rồi copy đúng tên vào lệnh trên.
Giải thích các cờ QUAN TRỌNG:
- `-ngl 99` : đẩy toàn bộ layer lên GPU (99 = "tất cả", nhanh nhất).
- `-c 16384` : context 16K — đủ cho Just Chatting, không tràn VRAM.
- `-fa on` : flash-attention, nhanh hơn, tiết kiệm VRAM.
- `--jinja` : dùng chat template chuẩn của model (quan trọng cho đúng format).
- `--reasoning-format none` : **TẮT thinking mode** — Qwen3.5 mặc định có thể
  sinh `<think>...</think>`, cờ này giúp llama.cpp tự xử lý gọn (nếu vẫn thấy
  `<think>` lọt ra, code Bước A sẽ strip thêm — xem llm_client.py).
- `--port 8000` : khớp với `LLM_BASE_URL` trong `.env.example`.

✅ Checkpoint: thấy dòng `server listening on http://0.0.0.0:8000`.
⚠️ Nếu OOM: hạ xuống `-ngl 40` (một phần CPU) hoặc dùng bản Q4_K_M thay vì Q6_K/Q8_0.

---

## BƯỚC 4 — Test server (terminal THỨ HAI, giữ server chạy)
```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-9b",
    "messages": [{"role": "user", "content": "Chào! Bạn là ai?"}],
    "max_tokens": 100,
    "temperature": 0.8
  }'
```
✅ Checkpoint: nhận JSON có câu trả lời tiếng Việt.
🎉 Server chạy được → sẵn sàng nối code Bước A (`modules/outputs/llm_client.py`).

---

## Ghi chú cho Tầng 3 (train — làm sau, vài tháng nữa)
- llama.cpp GGUF **không train trực tiếp được** (giống AWQ) — khi tới lúc train:
  1. Tải bản GỐC (safetensors, ~18GB) làm nền để QLoRA/DPO. Có 2 lựa chọn:
     - `HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive` (đúng nền đang chạy) — 
       khuyên dùng để giữ nhất quán giữa bản chạy live và bản train.
     - hoặc `Qwen/Qwen3.5-9B` gốc nếu muốn train từ đầu (không kế thừa uncensored).
  2. QLoRA/DPO bằng Unsloth (đã hỗ trợ sẵn cho dòng model GGUF này — xem
     `unsloth/Qwen3.5-9B-MTP-GGUF` model card: "có thể fine-tune ngay tại máy").
  3. Convert kết quả sang GGUF mới bằng `convert_hf_to_gguf.py` + `llama-quantize`
     — bước này chạy chủ yếu CPU+RAM, KHÔNG cần 20GB+ VRAM như convert AWQ.
  4. **Giữ nguyên patch tắt-thinking** khi convert (hoặc tự thêm cờ
     `--reasoning-format none` ở server nếu bản mới không có patch sẵn).
  5. Thay file `.gguf` đang chạy bằng bản mới (có version, rollback được).

## Xử lý lỗi thường gặp
| Lỗi | Cách sửa |
|---|---|
| Build lỗi thiếu CUDA | Cài CUDA toolkit for WSL từ NVIDIA (không phải driver) |
| `<think>` vẫn lọt ra text | Code Bước A strip bằng regex (xem llm_client.py) |
| CUDA OOM khi chạy | Hạ `-ngl` xuống, hoặc đổi bản Q4_K_M nhỏ hơn |
| Server chạy chậm bất thường | Kiểm tra `-ngl 99` có đúng chưa (layer có lên GPU không) |
| Tải model đứt giữa chừng | Chạy lại lệnh `huggingface-cli download`, nó tải tiếp |
