import os
RECV_PORT   = int(os.getenv("DASHBOARD_PORT", "8820"))
CONTROL_HOST= os.getenv("CONTROL_HOST", "127.0.0.1")
CONTROL_PORT= int(os.getenv("CONTROL_PORT", "8821"))