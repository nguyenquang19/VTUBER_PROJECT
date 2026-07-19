import time
def now_ms() -> float: return time.time() * 1000          # tuyệt đối (NTP) — chỉ dùng khi so giữa 2 máy
def mono_ms() -> float: return time.perf_counter() * 1000 # tương đối — đo nội bộ 1 máy