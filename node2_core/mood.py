import json
import os
import queue
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field

from .config import MOOD_DB_PATH, MOOD_LOG_PATH

MOOD_DIMS = ("vui", "buon", "buc", "bon_chon", "nguong")


@dataclass
class MoodState:
    vui: int = 0
    buon: int = 0
    buc: int = 0
    bon_chon: int = 0
    nguong: int = 0
    ly_do: str = ""
    updated_at: float = 0.0     # epoch seconds

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "MoodState":
        return MoodState(
            vui=int(d.get("vui", 0)),
            buon=int(d.get("buon", 0)),
            buc=int(d.get("buc", 0)),
            bon_chon=int(d.get("bon_chon", 0)),
            nguong=int(d.get("nguong", 0)),
            ly_do=str(d.get("ly_do", "")),
            updated_at=float(d.get("updated_at", 0.0)),
        )


# RAM giữ bản đang dùng, đọc/ghi liên tục. SQLite dự phòng (1 bảng, 1 dòng,
# ghi đè) đọc một lần lúc khởi động. jsonl ghi lịch sử, chỉ ghi thêm.
# Ghi SQLite + jsonl qua queue + luồng nền -> KHÔNG chặn đường chính,
# giống hệt kiểu shared/utils/trace_sink.py.
class MoodStore:
    def __init__(self, db_path=MOOD_DB_PATH, log_path=MOOD_LOG_PATH, maxlen=1000):
        self._db_path = db_path
        self._log_path = log_path
        self._lock = threading.Lock()
        self._current = self._load_from_sqlite()
        self._q: "queue.Queue" = queue.Queue(maxsize=maxlen)
        self._pump_thread = threading.Thread(target=self._pump, daemon=True)
        self._pump_thread.start()

    def get(self) -> MoodState:
        with self._lock:
            return self._current

    def set(self, mood: MoodState, turn_id: str = "", said: str = "") -> None:
        with self._lock:
            self._current = mood
        try:
            self._q.put_nowait((mood, turn_id, said))
        except queue.Full:
            pass   # bỏ, không chặn turn

    def age_seconds(self) -> float:
        with self._lock:
            updated_at = self._current.updated_at
        if updated_at <= 0:
            return 0.0
        return max(0.0, time.time() - updated_at)

    def close(self, timeout: float = 2.0) -> None:
        """Ghi hết hàng đợi rồi dừng thread nền. Dùng trong test để tránh
        thread còn ghi khi thư mục tạm bị xóa."""
        self._q.put(None)                      # sentinel dừng
        self._pump_thread.join(timeout=timeout)

    # ---- nền ----
    def _pump(self):
        while True:
            item = self._q.get()
            if item is None:                   # sentinel -> thoát
                return
            mood, turn_id, said = item
            try:
                self._write_sqlite(mood)
            except Exception:
                pass
            try:
                self._append_jsonl(mood, turn_id, said)
            except Exception:
                pass

    def _write_sqlite(self, mood: MoodState):
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mood ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "vui INT, buon INT, buc INT, bon_chon INT, nguong INT, "
                "ly_do TEXT, updated_at REAL)"
            )
            conn.execute(
                "INSERT INTO mood (id, vui, buon, buc, bon_chon, nguong, ly_do, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "vui=excluded.vui, buon=excluded.buon, buc=excluded.buc, "
                "bon_chon=excluded.bon_chon, nguong=excluded.nguong, "
                "ly_do=excluded.ly_do, updated_at=excluded.updated_at",
                (mood.vui, mood.buon, mood.buc, mood.bon_chon, mood.nguong,
                 mood.ly_do, mood.updated_at),
            )
            conn.commit()
        finally:
            conn.close()

    def _append_jsonl(self, mood: MoodState, turn_id: str, said: str):
        os.makedirs(os.path.dirname(self._log_path) or ".", exist_ok=True)
        item = mood.to_dict()
        item["turn_id"] = turn_id
        item["said"] = said
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _load_from_sqlite(self) -> MoodState:
        try:
            if not os.path.exists(self._db_path):
                return MoodState()
            conn = sqlite3.connect(self._db_path)
            try:
                row = conn.execute(
                    "SELECT vui, buon, buc, bon_chon, nguong, ly_do, updated_at "
                    "FROM mood WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()
            if not row:
                return MoodState()
            vui, buon, buc, bon_chon, nguong, ly_do, updated_at = row
            return MoodState(vui, buon, buc, bon_chon, nguong, ly_do or "", updated_at or 0.0)
        except Exception:
            return MoodState()