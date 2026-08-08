"""SQLite persistence for M7 relationship records; stores pseudonyms only."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from services.relationship.types import (
    NarrativeItem,
    NarrativeStatus,
    RelationshipNote,
    ReviewStatus,
    RunningGag,
    ViewerProfile,
)


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

    def update_profile_attributes(
        self, viewer_id: str, *, preferences: tuple[str, ...], boundaries: tuple[str, ...],
        tone: str | None, updated_at: datetime,
    ) -> ViewerProfile | None:
        self._conn.execute(
            """
            UPDATE viewer_profiles SET confirmed_preferences_json = ?, boundaries_json = ?,
                tone = ?, updated_at = ? WHERE viewer_id = ?
            """,
            (
                json.dumps(preferences, ensure_ascii=False),
                json.dumps(boundaries, ensure_ascii=False), tone,
                updated_at.isoformat(), viewer_id,
            ),
        )
        self._conn.commit()
        return self.get_profile(viewer_id)

    def insert_note(self, note: RelationshipNote) -> None:
        self._conn.execute(
            """
            INSERT INTO relationship_notes(
                note_id, viewer_id, summary, evidence_json, status, source,
                created_at, expires_at, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                note.note_id, note.viewer_id, note.summary,
                json.dumps(note.evidence_refs, ensure_ascii=False), note.status.value,
                note.source, note.created_at.isoformat(), note.expires_at.isoformat(),
                note.reviewed_at.isoformat() if note.reviewed_at else None,
            ),
        )
        self._conn.commit()

    def get_note(self, note_id: str) -> RelationshipNote | None:
        row = self._conn.execute(
            "SELECT * FROM relationship_notes WHERE note_id = ?", (note_id,),
        ).fetchone()
        return _note(row) if row is not None else None

    def list_notes(self, viewer_id: str | None = None) -> tuple[RelationshipNote, ...]:
        if viewer_id is None:
            rows = self._conn.execute(
                "SELECT * FROM relationship_notes ORDER BY created_at DESC, note_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM relationship_notes WHERE viewer_id = ? "
                "ORDER BY created_at DESC, note_id", (viewer_id,),
            ).fetchall()
        return tuple(_note(row) for row in rows)

    def review_note(self, note_id: str, status: ReviewStatus, reviewed_at: datetime) -> bool:
        cursor = self._conn.execute(
            "UPDATE relationship_notes SET status = ?, reviewed_at = ? WHERE note_id = ?",
            (status.value, reviewed_at.isoformat(), note_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_note(self, note_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM relationship_notes WHERE note_id = ?", (note_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def insert_narrative(self, item: NarrativeItem) -> None:
        self._conn.execute(
            """
            INSERT INTO narrative_items(
                narrative_id, viewer_id, summary, event_refs_json, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.narrative_id, item.viewer_id, item.summary,
                json.dumps(item.event_refs, ensure_ascii=False), item.status.value,
                item.created_at.isoformat(), item.expires_at.isoformat(),
            ),
        )
        self._conn.commit()

    def get_narrative(self, narrative_id: str) -> NarrativeItem | None:
        row = self._conn.execute(
            "SELECT * FROM narrative_items WHERE narrative_id = ?", (narrative_id,),
        ).fetchone()
        return _narrative(row) if row is not None else None

    def list_narratives(self) -> tuple[NarrativeItem, ...]:
        rows = self._conn.execute(
            "SELECT * FROM narrative_items ORDER BY created_at DESC, narrative_id"
        ).fetchall()
        return tuple(_narrative(row) for row in rows)

    def resolve_narrative(self, narrative_id: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE narrative_items SET status = ? WHERE narrative_id = ?",
            (NarrativeStatus.RESOLVED.value, narrative_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def write_audit(
        self, *, audit_id: str, action: str, viewer_id: str | None,
        target_id: str | None, reason: str, created_at: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO relationship_audit(audit_id, action, viewer_id, target_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (audit_id, action, viewer_id, target_id, reason, created_at.isoformat()),
        )
        self._conn.commit()

    def prune_seen_events(self, before: datetime) -> int:
        cursor = self._conn.execute(
            "DELETE FROM relationship_seen_events WHERE occurred_at < ?", (before.isoformat(),),
        )
        self._conn.commit()
        return max(0, cursor.rowcount)

    def record_positive_event(
        self, *, event_id: str, viewer_id: str, evidence_id: str, occurred_at: datetime,
    ) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO relationship_positive_events(event_id, viewer_id, evidence_id, occurred_at) "
            "VALUES (?, ?, ?, ?)",
            (event_id, viewer_id, evidence_id, occurred_at.isoformat()),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def positive_evidence(self, viewer_id: str) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT evidence_id FROM relationship_positive_events WHERE viewer_id = ? "
            "ORDER BY occurred_at, event_id", (viewer_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def insert_running_gag(self, gag: RunningGag) -> None:
        self._conn.execute(
            """
            INSERT INTO running_gags(
                gag_id, viewer_id, summary, event_refs_json, status, positive_count,
                created_at, last_referenced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gag.gag_id, gag.viewer_id, gag.summary,
                json.dumps(gag.event_refs, ensure_ascii=False), gag.status.value,
                gag.positive_count, gag.created_at.isoformat(),
                gag.last_referenced_at.isoformat() if gag.last_referenced_at else None,
            ),
        )
        self._conn.commit()

    def get_running_gag(self, gag_id: str) -> RunningGag | None:
        row = self._conn.execute(
            "SELECT * FROM running_gags WHERE gag_id = ?", (gag_id,),
        ).fetchone()
        return _running_gag(row) if row is not None else None

    def list_running_gags(self, viewer_id: str | None = None) -> tuple[RunningGag, ...]:
        if viewer_id is None:
            rows = self._conn.execute(
                "SELECT * FROM running_gags ORDER BY created_at DESC, gag_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM running_gags WHERE viewer_id = ? ORDER BY created_at DESC, gag_id",
                (viewer_id,),
            ).fetchall()
        return tuple(_running_gag(row) for row in rows)

    def review_running_gag(self, gag_id: str, status: ReviewStatus) -> bool:
        cursor = self._conn.execute(
            "UPDATE running_gags SET status = ? WHERE gag_id = ?",
            (status.value, gag_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def mark_running_gag_referenced(self, gag_id: str, when: datetime) -> bool:
        cursor = self._conn.execute(
            "UPDATE running_gags SET last_referenced_at = ? WHERE gag_id = ?",
            (when.isoformat(), gag_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0


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


def _note(row: sqlite3.Row) -> RelationshipNote:
    return RelationshipNote(
        note_id=str(row["note_id"]), viewer_id=str(row["viewer_id"]),
        summary=str(row["summary"]),
        evidence_refs=tuple(json.loads(row["evidence_json"] or "[]")),
        status=ReviewStatus(str(row["status"])), source=str(row["source"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        reviewed_at=(
            datetime.fromisoformat(str(row["reviewed_at"]))
            if row["reviewed_at"] is not None else None
        ),
    )


def _narrative(row: sqlite3.Row) -> NarrativeItem:
    return NarrativeItem(
        narrative_id=str(row["narrative_id"]),
        viewer_id=str(row["viewer_id"]) if row["viewer_id"] is not None else None,
        summary=str(row["summary"]),
        event_refs=tuple(json.loads(row["event_refs_json"] or "[]")),
        status=NarrativeStatus(str(row["status"])),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
    )


def _running_gag(row: sqlite3.Row) -> RunningGag:
    return RunningGag(
        gag_id=str(row["gag_id"]), viewer_id=str(row["viewer_id"]),
        summary=str(row["summary"]),
        event_refs=tuple(json.loads(row["event_refs_json"] or "[]")),
        status=ReviewStatus(str(row["status"])),
        positive_count=int(row["positive_count"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        last_referenced_at=(
            datetime.fromisoformat(str(row["last_referenced_at"]))
            if row["last_referenced_at"] is not None else None
        ),
    )
