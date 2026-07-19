import os
SENT_PORT   = int(os.getenv("NODE3_PORT", "8803"))       # khớp egress Node2
STOP_PORT   = int(os.getenv("NODE3_STOP_PORT", "8804"))
OUT_DIR     = os.getenv("AUDIO_OUT_DIR", "audio_out")
TTS_ENGINE  = os.getenv("TTS_ENGINE", "edge")            # edge | piper
TTS_VOICE   = os.getenv("TTS_VOICE", "vi-VN-HoaiMyNeural")
PIPER_BIN   = os.getenv("PIPER_BIN", "piper")
PIPER_MODEL = os.getenv("PIPER_MODEL", "")               # đường dẫn .onnx nếu dùng piper