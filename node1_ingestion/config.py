import os
RATE_WINDOW_S = float(os.getenv("RATE_WINDOW_S", "3"))   # tối thiểu giây/tin/user
NODE2_HOST = os.getenv("NODE2_HOST", "127.0.0.1")
NODE2_PORT = int(os.getenv("NODE2_PORT", "8802"))
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")