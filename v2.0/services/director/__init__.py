"""Director — đạo diễn stream (C0, docs/MAI_V2_SYSTEM_SPEC.md).

Biến Mai từ reactive chatbot (đáp mọi tin FIFO) thành host chủ động:
- SaliencePool (C0.1): chấm điểm + decay + cluster chat → nhặt tin đáng đáp.
- ChatPulse (C0.2): đo độ sôi nổi (tempo/diversity).
- Director loop (C0.3): segment state + action table quyết định làm gì.
"""
