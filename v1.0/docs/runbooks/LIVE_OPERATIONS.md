# Mai live operations runbook (M9)

## Trước khi live

1. Khởi động `llama-server.exe` qua runtime của Mai; không dùng Ollama.
2. Mở dashboard cùng runtime, hoặc mở độc lập bằng:
   `python scripts\dashboard_standalone.py`.
3. Xác nhận tab Agent hiển thị runtime online, health targets không mở circuit,
   action queue hợp lý và không còn incident chưa xử lý.
4. Dashboard độc lập chỉ đọc snapshot cuối; nút thay đổi trạng thái bị khóa khi runtime offline.

## Trong khi live

- `Pause agent` dừng Director nhưng giữ dashboard và quan sát hoạt động.
- `Emergency stop` là thao tác một nút: đóng speech/action gate trước, hủy TTS/audio,
  dừng Director và khóa auto-recovery.
- `Resume` prune goal hết TTL trước khi mở gate; không tiếp tục goal stale.
- Health supervisor chỉ thử restart tối đa 3 lần trong cửa sổ cấu hình, có exponential
  backoff. Khi circuit mở, operator phải xem incident, sửa nguyên nhân rồi mới reset.

M6/environment game đang được hoãn. Khi thêm executor thật, executor bắt buộc kiểm tra
`EmergencyController.permits_environment_action()` ngay trước side effect và cung cấp
callback hủy hành động đang chạy.

## Phân loại incident

- `info`: đã phục hồi, chỉ cần theo dõi.
- `warning`: degraded/operator alert; có thể tiếp tục nếu output vẫn an toàn.
- `critical`: restart thất bại hoặc circuit mở; pause/emergency stop và xử lý trước khi tiếp tục.

Incident ghi tại `logs\operations\incidents.jsonl`, schema version 1, append-only và đã
mask PII. Không dán chat, prompt, token hoặc thông tin đăng nhập vào summary/evidence.

## Tắt stream

1. Pause hoặc emergency stop nếu đang có output.
2. Dừng runtime bình thường để shutdown coordinator dừng recovery/driver/input/speech,
   đóng websocket, lưu snapshot và flush logger.
3. Không đóng cưỡng bức cửa sổ nếu graceful shutdown vẫn đang trong timeout cấu hình.

## Review và export sau stream

Chạy `python scripts\post_stream_review.py`. Lệnh xuất checklist metadata-only vào
`logs\operations\reviews` và trả exit code khác 0 nếu:

- snapshot shutdown thiếu/hỏng hoặc có lỗi trước snapshot;
- JSONL incident/audit lỗi parse;
- còn incident chưa resolve;
- acceptance soak M9 thiếu hoặc fail.

Checklist không export nội dung chat/prompt. Resolve incident bằng `IncidentLog.resolve()`
sau khi ghi rõ hành động khắc phục, rồi chạy lại review. Sao lưu các JSONL, shutdown snapshot
và review report theo chính sách lưu trữ của operator.

## Acceptance soak

`python scripts\soak_live_operations.py --duration 7200` chạy controlled input 2 giờ và
đo memory growth, queue high-water, p95 latency, error rate, deadlock và sequence/checksum
data loss. M9 chỉ đạt DoD khi mọi gate trong `docs\baselines\m9_live_operations.json` pass.
