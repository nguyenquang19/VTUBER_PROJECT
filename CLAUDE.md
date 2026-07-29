@"
# Project: Mai - AI VTuber

## Tài liệu chính
- ``docs/QUICKSTART.md`` — stack tổng quan, nguyên tắc
- ``docs/ARCHITECTURE.md`` — spec đầy đủ (đọc section liên quan task hiện tại)

## Ràng buộc bắt buộc (KHÔNG vi phạm)
1. **OS:** Windows 11 only. Dùng PowerShell, KHÔNG dùng Bash syntax.
2. **LLM backend:** llama.cpp (llama-server.exe), KHÔNG dùng Ollama, KHÔNG dùng transformers/vllm.
3. **Ngôn ngữ:** Python 3.11+, type hints đầy đủ, async/await cho I/O.
4. **Interface-based (P3):** mọi service implement interface trong ``interfaces/``.
5. **Feature toggle (P1):** mọi feature mới register vào FeatureManager.
6. **Observable (P2):** mọi feature có ít nhất 1 metric (Section 5.3).
7. **Config over code (P5):** không hardcode magic numbers, dùng YAML config.
8. **Simplicity (P6):** làm bản đơn giản trước, add complexity khi cần thật.

## Ranh giới NGHIÊM CẤM
- KHÔNG tạo code chưa test được.
- KHÔNG tạo file ngoài scope task đang được giao.
- KHÔNG tự nhảy sang task tiếp theo nếu chưa được confirm.
- KHÔNG dùng Bash command (rm, cp, mkdir -p, source, /) — dùng PowerShell.
- KHÔNG dùng SIGTERM/SIGKILL — Windows dùng ``proc.terminate()`` là hard-kill.
- KHÔNG copy code từ v1.0/v2.0 vì đã bị deprecate.

## Workflow bắt buộc
Trước khi code bất kỳ file nào:
1. Xác nhận đã đọc section ARCHITECTURE.md tương ứng
2. List file sẽ tạo/sửa
3. List test sẽ viết
4. Confirm với user rồi mới code

Sau khi code xong:
1. Chạy pytest cho test tương ứng, show output
2. STOP và báo user review
3. KHÔNG tự động sang task tiếp theo

## Phase hiện tại
Xem file ``PHASE.md`` ở root để biết đang ở phase nào.
"@ | Out-File CLAUDE.md -Encoding utf8