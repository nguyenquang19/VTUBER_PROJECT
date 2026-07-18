# Kiến trúc AI VTuber

## Triết lý
Nhiều khối chạy song song, giao tiếp qua **EventBus** (pub/sub). Mỗi khối
thay thế được (swappable) mà không phá khối khác. Mock → thật không đổi logic.

## Luồng chính (V1 — Discord-only)
```
Discord chat ─► [inputs] ─► EventBus ─► [Orchestrator] ─┬─► [LLM] ─► câu
                                            │            │
                              [MonologueEngine]          ▼
                              (tự nói có mạch)      [TTS] ─► audio
                                                         │
                                        [Avatar/VTS] ◄───┴─► OBS
```

## "Đảo ngược tư duy" — linh hồn livestream
AI LUÔN có mạch tự nói (MonologueEngine) làm nền. Chat CHEN VÀO mạch đó.
Orchestrator quyết định turn-taking hai chiều: tự nói ↔ trả lời chat ↔ barge-in.

## Các tầng học (biết lớn lên)
- Tầng 1: context trong buổi (short-term).
- Tầng 2: memory + reflection sau mỗi buổi → nạp topic cho monologue. **Chính.**
- Tầng 3: DPO-LoRA offline hàng tháng (V2).

## Persona 2 tầng
- Core (bất biến): hiến pháp, AI không tự sửa.
- Evolving: reflection cập nhật, người review tuần, rollback được.

## Phân bổ máy
- Máy A (5060 Ti, WSL2): LLM (vLLM) + TTS + Orchestrator + Discord + Memory.
- Máy B (3050, Windows): VTube Studio + OBS.
- Máy C (3050, WiFi): để dành V2 (vision/hát) — V1 chưa cần.
```
