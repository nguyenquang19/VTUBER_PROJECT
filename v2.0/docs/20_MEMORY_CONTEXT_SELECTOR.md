# 20 — Memory và Context Selector

> **Applies to:** implementation generation `v2.0`; rollout mặc định tắt qua `context_selector`.
>
> **Phase:** 12 — bounded, grounded prompt composition; không thay Director, delivery hay memory-write policy.

## Mục tiêu và boundary

`ConversationContextComposer` là pipeline context duy nhất cho một LLM turn. Khi feature
`context_selector` bật, nó chọn một context bounded từ public snapshots thay vì dump state:

1. World Model: current truth còn TTL, có source, confidence và evidence refs.
2. Self Model: trạng thái vận hành tối thiểu (busy/degraded/topic/goal/thread).
3. Memory: tối đa `memory_items` entry truy hồi bằng query hiện tại và lọc `viewer_id` nếu có.
4. Relationship, active goal, open thread, recap và grounded event: các phần continuity hiện có.
5. Capability registry: chỉ các capability currently available, không kèm executor/callable.

Composer chỉ đọc snapshot hoặc `MemoryService.query`; nó không ghi memory, không reserve action,
không đổi Director decision, và không cấp executor route. Nếu reader lỗi/timeout, phần đó vắng mặt và
turn vẫn tiếp tục với grounded state sẵn có.

## Grounding rules

- World Model là current truth. Khi World có key trùng key được memory metadata khai báo
  (`world_path`), memory đó bị loại; memory không override state World còn tươi.
- Mỗi World item giữ `source`, `confidence`, `updated_at` và `evidence_refs`; không suy diễn khi
  thiếu evidence.
- Memory chỉ được render là `Past memory`, không được gắn nhãn delivered success. Entry có
  `metadata.action_status` khác `success`/`delivered` được ghi rõ là failed/unknown outcome.
- Nếu query memory lỗi hoặc trả rỗng, selector không bịa fact để bù.
- Tổng context, số world/memory/capability và độ dài từng item đều lấy từ
  `config/conversation.yaml::context_selector`; mọi section có giới hạn riêng.

## Rollout và quan sát

`context_selector` phụ thuộc `conversation_continuity` và mặc định `false`. Tắt feature lập tức dùng
renderer continuity trước Phase 12. Bật/tắt không thay memory storage hay lịch sử prompt.

Metric service:

- `conversation_context_selector_renders_total`;
- `conversation_context_selector_memory_items_total`;
- `conversation_context_selector_world_items_total`;
- `conversation_context_selector_memory_errors_total`;
- `conversation_context_selector_world_override_total`.

## Verification

Unit test phải chứng minh: context bị giới hạn; World fresh thắng memory conflict; provenance và
confidence xuất hiện; action failure không thành success; lỗi memory fail-open; và feature toggle vẫn
rollback về continuity renderer cũ. Chạy targeted context/memory/runtime tests rồi full offline pytest.