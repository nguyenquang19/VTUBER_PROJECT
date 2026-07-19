# Sinh .vtt đồng bộ theo duration thật của từng câu. LUÔN bật, không chỉ khi lỗi.
def _ts(ms: float) -> str:
    s, ms = divmod(int(ms), 1000); m, s = divmod(s, 60); h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

class VttWriter:
    def __init__(self, path: str):
        self._path = path; self._cursor = 0.0; self._n = 0
        with open(path, "w", encoding="utf-8") as f: f.write("WEBVTT\n\n")
    def add(self, text: str, duration_ms: float):
        start, end = self._cursor, self._cursor + duration_ms
        self._cursor = end; self._n += 1
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(f"{self._n}\n{_ts(start)} --> {_ts(end)}\n{text}\n\n")