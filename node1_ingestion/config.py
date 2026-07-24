import os
RATE_WINDOW_S = float(os.getenv("RATE_WINDOW_S", "3"))   # tối thiểu giây/tin/user
NODE2_HOST = os.getenv("NODE2_HOST", "127.0.0.1")
NODE2_PORT = int(os.getenv("NODE2_PORT", "8802"))
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ROOM_WINDOW_S = 10.0    # giây, cửa sổ đếm nhịp phòng cho giới hạn tốc độ
ROOM_MSG_LIMIT = 30     # gợi ý ban đầu — chỉnh sau khi chạy thật
ROOM_HIGH_SCORE = 0.5   # coi là "cao" khi có từ khóa ?/help/cứu/giúp (tier1_score hiện chỉ có 4 mức: 0.1/0.3/0.5/0.7)
