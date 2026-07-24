import os

# TẤT CẢ là SỐ KHỞI ĐIỂM (nguyên tắc 4) — chỉnh sau khi chạy thật và NGHE.
# Đừng cố tìm số đúng trên giấy.

TICK_SEC = float(os.getenv("NODE6_TICK_SEC", "2.0"))     # nhịp quét

# Gửi việc AUTO vào ingress node2 (cùng cổng node1 dùng).
NODE2_HOST = os.getenv("NODE2_HOST", "127.0.0.1")
NODE2_PORT = int(os.getenv("NODE2_PORT", "8802"))

# Đọc mood — CHỈ đọc, cùng file node2 ghi (DAC-TA 4.5 cách b).
MOOD_DB_PATH = os.getenv("MOOD_DB_PATH", "data/mood.db")

# Nhịp phòng + voice do node1 công bố (best-effort; thiếu file -> mặc định an toàn).
ROOM_STATE_PATH = os.getenv("NODE6_ROOM_STATE", "data/room_state.json")

# --- PHANH AN TOÀN: thứ DUY NHẤT cứng, KHÔNG phụ thuộc cảm xúc (he-thong-logic §5) ---
MAX_AUTO_STREAK = int(os.getenv("NODE6_MAX_AUTO_STREAK", "5"))

# --- urge: các yếu tố đẩy mức muốn nói lên/xuống (§4) ---
URGE_CHAT_SILENT_PER_MIN = 3.0      # chat im mỗi phút -> cộng dần
URGE_MAI_SILENT_PER_MIN = 1.0       # Mai im mỗi phút -> cộng nhẹ
URGE_JUST_SPOKE_CUTOFF_SEC = 20.0   # vừa nói xong (< ngần này giây) -> tụt mạnh
URGE_JUST_SPOKE_DAMP = 0.2          # hệ số nhân khi vừa nói
URGE_JUST_JOINED_BONUS = 6.0        # có người mới vào -> nhảy vọt
URGE_PENDING_TOPIC_BONUS = 4.0      # đang kể dở (model báo "còn nữa") -> giữ cao
URGE_ROOM_BUSY_MSGS = 8             # >= tin/30s coi là chat đang sôi
URGE_ROOM_BUSY_PENALTY = 5.0        # chat đang sôi -> giảm (chen vào là phá)

# --- threshold: mốc nền + mood điều khiển ngưỡng (§4) ---
THRESHOLD_BASE = 10.0
THRESHOLD_BON_CHON_PER_POINT = -0.6   # bồn_chồn cao -> hạ ngưỡng (sốt ruột, dễ nói)
THRESHOLD_VUI_PER_POINT = -0.4        # vui cao -> hạ ngưỡng (hay nói)
THRESHOLD_BUON_PER_POINT = 0.6        # buồn cao -> nâng ngưỡng (im lâu hơn)
THRESHOLD_NGUONG_PER_POINT = 0.5      # ngượng cao -> nâng ngưỡng (rụt)
THRESHOLD_NOISE_FRAC = 0.13           # ±13% nhiễu ngẫu nhiên -> nhịp không đoán được
THRESHOLD_MIN = 2.0                   # sàn, không cho ngưỡng xuống quá thấp
