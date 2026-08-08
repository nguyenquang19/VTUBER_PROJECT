"""SQLite persistence for M7 relationship records; stores pseudonyms only."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from services.relationship.types import ViewerProfile


class RelationshipStore:
    def __init__(
        self, db_path: str | Path | None = None, *, conn: sqlite3.Connection | None = None,
    ) -> None:
        if conn is None and db_path is None:
            raise ValueError("db_path or conn is required")
        self._conn = conn or sqlite3.connect(str(db_path), check_same_thread=False)
        self._owns_conn = conn is None
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    def observe_profile(
        self, *, viewer_id: str, event_id: str, occurred_at: datetime, expires_at: datetime,
    ) -> tuple[ViewerProfile, bool]:
        """Return profile and whether this event increased the interaction count."""
        try:
            inserted = self._conn.execute(
                "INSERT OR IGNORE INTO relationship_seen_events(event_id, viewer_id, occurred_at) "
                "VALUES (?, ?, ?)",
                (event_id, viewer_id, occurred_at.isoformat()),
            ).rowcount > 0
            if inserted:
                self._conn.execute(
                    """
                    INSERT INTO viewer_profiles(
                        viewer_id, interaction_count, first_seen, last_seen, expires_at,
                        confirmed_preferences_json, boundaries_json, tone, updated_at
                    ) VALUES (?, 1, ?, ?, ?, '[]', '[]', NULL, ?)
                    ON CONFLICT(viewer_id) DO UPDATE SET
                        interaction_count = viewer_profiles.interaction_count + 1,
                        last_seen = excluded.last_seen,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        viewer_id, occurred_at.isoformat(), occurred_at.isoformat(),
                        expires_at.isoformat(), occurred_at.isoformat(),
                    ),
                )
            row = self._conn.execute(
                "SELECT * FROM viewer_profiles WHERE viewer_id = ?", (viewer_id,),
            ).fetchone()
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        if row is None:
            raise RuntimeError("profile was not created")
        return _profile(row), inserted

    def get_profile(self, viewer_id: str) -> ViewerProfile | None:
        row = self._conn.execute(
            "SELECT * FROM viewer_profiles WHERE viewer_id = ?", (viewer_id,),
        ).fetchone()
        return _profile(row) if row is not None else None

    def list_profiles(self) -> tuple[ViewerProfile, ...]:
        rows = self._conn.execute(
            "SELECT * FROM viewer_profiles ORDER BY last_seen DESC, viewer_id"
        ).fetchall()
        return tuple(_profile(row) for row in rows)

    def prune_seen_events(self, before: datetime) -> int:
        cursor = self._conn.execute(
            "DELETE FROM relationship_seen_events WHERE occurred_at < ?", (before.isoformat(),),
        )
        self._conn.commit()
        return max(0, cursor.rowcount)


def _profile(row: sqlite3.Row) -> ViewerProfile:
    return ViewerProfile(
        viewer_id=str(row["viewer_id"]),
        interaction_count=int(row["interaction_count"]),
        first_seen=datetime.fromisoformat(str(row["first_seen"])),
        last_seen=datetime.fromisoformat(str(row["last_seen"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        confirmed_preferences=tuple(json.loads(row["confirmed_preferences_json"] or "[]")),
        boundaries=tuple(json.loads(row["boundaries_json"] or "[]")),
        tone=str(row["tone"]) if row["tone"] is not None else None,
    )

