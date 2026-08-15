# 21 — Embodiment Policy

> **Applies to:** implementation generation `v2.0`; feature `embodiment_policy` mặc định tắt.
>
> **Phase:** 13 — avatar behavior bounded, delivery-aware and fail-safe.

## Ownership and levels

Embodiment Policy là lớp điều phối duy nhất giữa delivery và avatar adapter. Nó không tạo fact,
không đổi mood, không thay Director priority, không commit transaction/historical state và không gọi
LLM/TTS.

| Level | Examples | Owner | Rule |
|---|---|---|---|
| LOW | blink, lip-sync, idle | VTube Studio/model | automatic, không có runtime action |
| MID | mood expression, posture, gaze | `EmbodimentPolicy.apply_mid` | chỉ sau `TTSDeliveryResult.delivered=true` |
| HIGH | wave, celebrate, intentional gesture | typed `ActionRequest` → `AvatarGestureExecutor` | phải có evidence, policy lease và VTS acknowledgement |

## Safety and delivery boundary

MID biểu cảm chỉ là cosmetic hậu delivery: delivery failure, cancellation, filter rejection hoặc sink
thiếu không tạo embodiment side effect. Policy dùng cooldown YAML và active lease để không chạy MID
song song HIGH hoặc lặp mood quá nhanh.

HIGH là action attempt, không phải suy luận rằng avatar đã diễn xong. `AvatarGestureExecutor` chỉ gọi VTS
sau khi policy cấp lease. Concurrent/high action overlap, cooldown, missing evidence, adapter disabled,
VTS disconnected hoặc VTS reject đều là outcome fail-safe. Result data giữ `gesture_id` và bounded
`evidence_refs`; only VTS acknowledgement may be verified by `AvatarGestureVerifier`.

## Configuration and rollout

`animation.yaml::embodiment` đặt `mid_cooldown_s`, `intentional_cooldown_s`,
`max_evidence_refs` và `max_recent_records`. `embodiment_policy` phụ thuộc `animation_smooth` và mặc
định tắt; tắt feature giữ direct automatic MID expression của legacy delivery path. LOW không đổi.

## Observability

Policy giữ bounded records và counters: mid applied/cooldown/conflict, intentional started/verified/
failed/rejected, cùng số active lease. VTS service metrics vẫn là nguồn chẩn đoán kết nối/hotkey.

## Verification

Tests chứng minh MID chỉ chạy sau delivery, mood không tạo fact/hard priority, HIGH cần evidence,
concurrent intentional action bị từ chối, failure không được đánh verified, và VTS degraded trả fail-safe.