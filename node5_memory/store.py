import sqlite3, threading, time, os
from .schema import UserProfile, ModStatus

DB_PATH = os.getenv("NODE5_DB", "data/node5.db")

class Store:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init()
    def _init(self):
        with self._lock:
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS profiles(
              user_id TEXT PRIMARY KEY, display_name TEXT, first_seen REAL,
              appearances INTEGER, summary TEXT, last_mentioned REAL,
              memory_strength REAL, mod_status TEXT);
            CREATE TABLE IF NOT EXISTS mod_log(
              user_id TEXT, session_id TEXT, ts REAL, old_summary TEXT,
              new_summary TEXT, decision TEXT, source TEXT);
            -- chống ghi trùng theo (user, session): idempotent
            CREATE UNIQUE INDEX IF NOT EXISTS ux_written
              ON mod_log(user_id, session_id) WHERE source='auto_endsession';
            """); self._db.commit()

    def get_profile(self, user_id: str):
        with self._lock:
            r = self._db.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        if not r: return None
        return UserProfile(r[0], r[1], r[2], r[3], r[4], r[5], r[6], ModStatus(r[7]))

    def upsert_profile(self, p: UserProfile):
        with self._lock:
            self._db.execute("""INSERT INTO profiles VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name,
                appearances=excluded.appearances, summary=excluded.summary,
                last_mentioned=excluded.last_mentioned, memory_strength=excluded.memory_strength,
                mod_status=excluded.mod_status""",
                (p.user_id,p.display_name,p.first_seen,p.appearances,p.summary,
                 p.last_mentioned,p.memory_strength,p.mod_status.value))
            self._db.commit()

    def already_written(self, user_id: str, session_id: str) -> bool:
        with self._lock:
            r = self._db.execute(
                "SELECT 1 FROM mod_log WHERE user_id=? AND session_id=? AND source='auto_endsession'",
                (user_id, session_id)).fetchone()
        return r is not None

    def log_moderation(self, log):
        with self._lock:
            try:
                self._db.execute("INSERT INTO mod_log VALUES(?,?,?,?,?,?,?)",
                    (log.user_id,log.session_id,log.ts,log.old_summary,
                     log.new_summary,log.decision,log.source))
                self._db.commit()
            except sqlite3.IntegrityError:
                pass   # đã ghi (idempotent) -> bỏ qua, không tạo bản trùng