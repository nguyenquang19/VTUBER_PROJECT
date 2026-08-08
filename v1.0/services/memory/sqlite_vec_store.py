"""SqliteVecStore — wrapper thao tác SQLite + sqlite-vec (Phase 7.B).

Tách khỏi service logic để test độc lập không cần embedding model.
Sync API (SQLite là sync); SemanticMemoryService (7.D) wrap trong asyncio.to_thread
+ áp timeout 150ms.

Schema đã tạo bởi migration 004 (services/memory/../migrations/004):
  memory_entries: entry_id/content/timestamp/tier/importance/tags_json/
                  metadata_json/viewer_id/session_id
  memory_vectors: entry_id / embedding float[1024]  (vec0, bge-m3 dim=1024)

Insert atomic 2 bảng (memory_entries + memory_vectors) trong 1 transaction.
Idempotent: entry_id trùng → REPLACE (không raise).
"""
from __future__ import annotations

import json
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


@dataclass
class StoredEntry:
    entry_id: str
    content: str
    timestamp: datetime
    tier: str
    importance: float
    tags: list[str]
    metadata: dict[str, Any]
    viewer_id: str | None
    session_id: str | None
    distance: float | None = None  # chỉ set khi trả từ knn query


class SqliteVecStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Truyền `db_path` HOẶC `conn` (test inject in-memory connection).

        Store TỰ load sqlite-vec extension — không phụ thuộc migration_runner
        (test có thể tạo store trên DB mới không qua runner).
        """
        if conn is not None:
            self._conn = conn
            self._owns_conn = False
        else:
            if db_path is None:
                raise ValueError("cần db_path HOẶC conn")
            # check_same_thread=False: SemanticMemoryService.query chạy store ops
            # qua asyncio.to_thread (khác thread tạo conn). Đây là pattern chuẩn
            # SQLite + asyncio. Concurrent access an toàn vì asyncio single-loop
            # serialize các coroutine — không có 2 to_thread cùng lúc trên 1 conn.
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
            self._owns_conn = True
        self._load_vec_extension()

    def _load_vec_extension(self) -> None:
        try:
            import sqlite_vec  # type: ignore

            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
        except Exception as e:
            raise RuntimeError(f"không load được sqlite-vec: {e}") from e

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    # ---------- write ----------

    def insert(
        self,
        entry_id: str,
        content: str,
        embedding: Sequence[float],
        *,
        timestamp: datetime | None = None,
        tier: str = "persistent",
        importance: float = 0.5,
        tags: Iterable[str] | None = None,
        metadata: dict[str, Any] | None = None,
        viewer_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Insert entry + embedding. Idempotent (REPLACE nếu trùng entry_id)."""
        ts = timestamp or datetime.now()
        tags_json = json.dumps(list(tags) if tags else [], ensure_ascii=False)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        emb_blob = _encode_vec(embedding)

        try:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO memory_entries
                    (entry_id, content, timestamp, tier, importance,
                     tags_json, metadata_json, viewer_id, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (entry_id, content, ts.isoformat(), tier, float(importance),
                 tags_json, meta_json, viewer_id, session_id),
            )
            # vec0 không hỗ trợ ON CONFLICT — DELETE + INSERT thủ công
            self._conn.execute(
                "DELETE FROM memory_vectors WHERE entry_id = ?", (entry_id,)
            )
            self._conn.execute(
                "INSERT INTO memory_vectors(entry_id, embedding) VALUES (?, ?)",
                (entry_id, emb_blob),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---------- read ----------

    def fetch_by_id(self, entry_id: str) -> StoredEntry | None:
        row = self._conn.execute(
            """
            SELECT entry_id, content, timestamp, tier, importance,
                   tags_json, metadata_json, viewer_id, session_id
              FROM memory_entries
             WHERE entry_id = ?
            """,
            (entry_id,),
        ).fetchone()
        return _row_to_entry(row) if row else None

    def query_knn(
        self,
        query_embedding: Sequence[float],
        top_k: int = 3,
        *,
        tier: str | None = None,
        viewer_id: str | None = None,
    ) -> list[StoredEntry]:
        """K-nearest-neighbor search theo cosine distance.

        Filter tier/viewer_id ở JOIN layer (post-KNN) — vec0 chưa hỗ trợ WHERE
        không phải MATCH ở query. Nếu filter loại nhiều entry, tăng top_k trước
        (over-fetch) để bù. Đơn giản trước, tune sau nếu recall giảm.
        """
        emb_blob = _encode_vec(query_embedding)
        # Over-fetch 3x khi có filter để bù entries bị loại post-KNN
        fetch_k = top_k * 3 if (tier or viewer_id) else top_k

        rows = self._conn.execute(
            """
            SELECT e.entry_id, e.content, e.timestamp, e.tier, e.importance,
                   e.tags_json, e.metadata_json, e.viewer_id, e.session_id,
                   v.distance
              FROM memory_vectors v
              JOIN memory_entries e ON e.entry_id = v.entry_id
             WHERE v.embedding MATCH ? AND v.k = ?
             ORDER BY v.distance
            """,
            (emb_blob, fetch_k),
        ).fetchall()

        results: list[StoredEntry] = []
        for row in rows:
            entry = _row_to_entry(row[:-1])
            if entry is None:
                continue
            if tier is not None and entry.tier != tier:
                continue
            if viewer_id is not None and entry.viewer_id != viewer_id:
                continue
            entry.distance = float(row[-1])
            results.append(entry)
            if len(results) >= top_k:
                break
        return results

    def count(self, *, tier: str | None = None) -> int:
        if tier is not None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM memory_entries WHERE tier = ?", (tier,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()
        return int(row[0]) if row else 0

    def list_by_viewer(self, viewer_id: str) -> list[StoredEntry]:
        rows = self._conn.execute(
            """
            SELECT entry_id, content, timestamp, tier, importance,
                   tags_json, metadata_json, viewer_id, session_id
              FROM memory_entries WHERE viewer_id = ? ORDER BY timestamp, entry_id
            """,
            (viewer_id,),
        ).fetchall()
        return [entry for row in rows if (entry := _row_to_entry(row)) is not None]

    # ---------- delete ----------

    def delete(self, entry_id: str) -> bool:
        """Trả True nếu xoá thực sự (entry tồn tại), False nếu không."""
        cur = self._conn.execute(
            "DELETE FROM memory_entries WHERE entry_id = ?", (entry_id,)
        )
        self._conn.execute(
            "DELETE FROM memory_vectors WHERE entry_id = ?", (entry_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_by_viewer(self, viewer_id: str) -> int:
        """Atomically delete metadata and vectors owned by one pseudonymous viewer."""
        rows = self._conn.execute(
            "SELECT entry_id FROM memory_entries WHERE viewer_id = ?", (viewer_id,),
        ).fetchall()
        try:
            for (entry_id,) in rows:
                self._conn.execute("DELETE FROM memory_vectors WHERE entry_id = ?", (entry_id,))
            self._conn.execute("DELETE FROM memory_entries WHERE viewer_id = ?", (viewer_id,))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return len(rows)


# ---------- helpers ----------


def _encode_vec(vec: Sequence[float]) -> bytes:
    """sqlite-vec vec0 nhận blob float32 little-endian."""
    arr = list(vec)
    return struct.pack(f"{len(arr)}f", *arr)


def _row_to_entry(row: tuple | None) -> StoredEntry | None:
    if row is None:
        return None
    (entry_id, content, ts, tier, importance,
     tags_json, meta_json, viewer_id, session_id) = row
    return StoredEntry(
        entry_id=entry_id,
        content=content,
        timestamp=datetime.fromisoformat(ts),
        tier=tier,
        importance=float(importance) if importance is not None else 0.5,
        tags=json.loads(tags_json) if tags_json else [],
        metadata=json.loads(meta_json) if meta_json else {},
        viewer_id=viewer_id,
        session_id=session_id,
    )
