# 3 NHÓM từ khóa. ĐỘI NGŨ TỰ ĐIỀN dựa trên cộng đồng thật — KHÔNG để AI đoán
# danh sách nhạy cảm này. Đây chỉ là khung mồi tối thiểu để chạy được.
# Nhóm tự hại/tự tử: NGƯỠNG THẤP NHẤT (dễ trigger nhất, thà nhầm còn hơn bỏ sót).

SELF_HARM = {   # ngưỡng thấp nhất
    "tự tử", "tự sát", "kết thúc tất cả", "không muốn sống",
    "chết đi cho rồi", "biến mất khỏi thế giới", "kết liễu",
    "tự làm hại", "rạch tay", "muốn chết",
}

VIOLENCE = {    # bạo lực nhắm vào người cụ thể
    "giết", "cho mày chết", "đâm chết", "tao sẽ tìm mày",
    "địa chỉ nhà mày", "đánh chết",
}

PLATFORM_SEVERE = {  # vi phạm nền tảng nghiêm trọng (đội ngũ tự bổ sung)
    # để trống/điền theo policy Discord + cộng đồng
}

# Ngưỡng riêng mỗi nhóm: self-harm nhạy nhất
THRESHOLDS = {
    "self_harm": 0.30,   # thấp nhất
    "violence": 0.50,
    "platform": 0.60,
}
# Mẫu đồng-xuất-hiện cho self-harm: mỗi tuple = các từ phải CÙNG có trong câu.
# ĐỘI NGŨ tự thêm dựa trên câu thật bị bỏ sót. Đây là cách bắt biến thể khác chữ
# mà keyword cụm cứng không bắt được.
# Ví dụ suy ra từ 2 câu vừa lọt (bạn kiểm và điều chỉnh):
SELF_HARM_PATTERNS = [
    ("lý do", "sống"),        # "không còn lý do gì để sống"
    ("không", "sống", "nữa"), # "không muốn sống nữa" / "không còn ... sống nữa"
    ("ai quan tâm", "chết"),  # "chẳng ai quan tâm nếu tôi chết"
    # thêm mẫu từ các câu bỏ sót khác...
]