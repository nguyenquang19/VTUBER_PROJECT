"""10 câu mẫu tiếng Việt cho Day 2 TTS quality test.

Phủ các case dễ vỡ với TTS: chào hỏi, cảm xúc, câu hỏi, câu dài,
số/tiền, tên riêng, dấu câu phức tạp, ngắt câu, viết tắt.
"""
from __future__ import annotations

SENTENCES: list[tuple[str, str]] = [
    ("01_greeting_short", "Xin chào, mình là Mai. Rất vui được gặp bạn hôm nay."),
    ("02_emotion_happy", "Wow tuyệt quá! Bạn giỏi thật đấy, mình phục bạn quá đi mất!"),
    ("03_emotion_sad", "Ừm... nghe buồn ghê. Mình hiểu cảm giác đó, đôi khi mọi thứ cứ nặng nề."),
    ("04_question", "Bạn nghĩ sao về việc học một thứ mới mỗi ngày? Có mệt lắm không?"),
    ("05_long_sentence",
     "Hôm nay trời trong xanh và mát mẻ, gió thổi nhè nhẹ qua tán cây phượng, "
     "buổi sáng thức dậy nghe tiếng chim hót, tự dưng thấy nhẹ nhõm và vui vẻ hơn hẳn."),
    ("06_numbers_money", "Tổng cộng là 1.250.000 đồng, bạn muốn thanh toán bằng thẻ hay tiền mặt?"),
    ("07_names_english",
     "Bạn thử xem YouTube kênh của Anthropic hoặc DeepMind chưa? Nội dung về AI khá hay."),
    ("08_exclamation", "Trời ơi! Không thể tin được! Bạn nói thật đấy à?"),
    ("09_ellipsis_pause",
     "Ừm... để mình nghĩ xíu... à đúng rồi, mình nhớ ra rồi. Là chuyện tuần trước mà."),
    ("10_mixed_punct",
     "Này, bạn ơi (nghe không?) — hôm nay mình đi ăn phở, uống trà sữa, xong ghé cà phê; "
     "cả ngày vui hết cỡ luôn!"),
]
