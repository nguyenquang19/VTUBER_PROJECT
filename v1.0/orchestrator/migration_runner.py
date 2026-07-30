"""Versioned SQL migration runner (ARCHITECTURE 8.8.2).

Đơn giản hơn Alembic, đủ cho solo dev (Appendix C).
- Migration file: `NNN_description.sql`, chạy theo thứ tự version.
- Track ở table `schema_migrations`.
- Backup DB trước MỖI migration (8.8.3) — dùng shutil (cross-platform),
  không shell ra PowerShell.
- Idempotent: file đã applied thì skip.

Rule 8.8.4: migration chỉ THÊM. Runner không hỗ trợ auto-rollback (risky với
SQLite) — fail thì restore từ backup, sửa script, retry.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.logger import get_logger

_VERSION_RE = re.compile(r"^(\d+)_.*\.sql$")


class MigrationError(Exception):
    pass


class MigrationRunner:
    def __init__(
        self,
        db_path: str | Path,
        migrations_dir: str | Path = "migrations",
        backup_dir: str | Path = "backups",
    ) -> None:
        self.db_path = Path(db_path)
        self.migrations_dir = Path(migrations_dir)
        self.backup_dir = Path(backup_dir)
        self._log = get_logger("migration_runner")

    # ---------- public ----------

    def initialize(self) -> list[str]:
        """Chạy mọi migration pending. Trả list version đã apply lần này."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_migrations_table()
        pending = self.get_pending()
        applied: list[str] = []
        if pending:
            self._log.info("running_migrations", count=len(pending))
        for filename in pending:
            self._backup_before_migration()
            self._apply(filename)
            applied.append(self._version_of(filename))
        return applied

    def get_applied_versions(self) -> set[str]:
        if not self.db_path.exists():
            return set()
        conn = sqlite3.connect(self.db_path)
        try:
            self._ensure_migrations_table(conn)
            rows = conn.execute(
                "SELECT version FROM schema_migrations WHERE success = 1"
            ).fetchall()
            return {r[0] for r in rows}
        finally:
            conn.close()

    def get_pending(self) -> list[str]:
        applied = self.get_applied_versions()
        files = self._migration_files()
        return [f for f in files if self._version_of(f) not in applied]

    def current_version(self) -> str | None:
        applied = self.get_applied_versions()
        return max(applied) if applied else None

    # ---------- internals ----------

    def _migration_files(self) -> list[str]:
        if not self.migrations_dir.exists():
            return []
        files = [f.name for f in self.migrations_dir.iterdir() if _VERSION_RE.match(f.name)]
        # sort theo version số, không phải lexicographic (để 010 sau 009)
        return sorted(files, key=lambda f: int(self._version_of(f)))

    @staticmethod
    def _version_of(filename: str) -> str:
        m = _VERSION_RE.match(filename)
        if not m:
            raise MigrationError(f"Tên migration không hợp lệ: {filename} (cần NNN_*.sql)")
        return m.group(1)

    def _ensure_migrations_table(self, conn: sqlite3.Connection | None = None) -> None:
        own = conn is None
        conn = conn or sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at DATETIME NOT NULL,
                    success INTEGER
                )
                """
            )
            conn.commit()
        finally:
            if own:
                conn.close()

    def _backup_before_migration(self) -> Path | None:
        """Copy DB hiện tại sang backups/. None nếu DB chưa tồn tại (lần đầu)."""
        if not self.db_path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest = self.backup_dir / f"{self.db_path.name}.pre_migration_{ts}"
        shutil.copy2(self.db_path, dest)
        self._log.info("migration_backup", backup=str(dest))
        return dest

    def _apply(self, filename: str) -> None:
        version = self._version_of(filename)
        sql = (self.migrations_dir / filename).read_text(encoding="utf-8")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (version, applied_at, success) "
                "VALUES (?, ?, 1)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            self._log.info("migration_applied", version=version)
        except Exception as e:
            conn.rollback()
            # Ghi lại lần fail để dashboard/log thấy (success=0)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, applied_at, success) "
                    "VALUES (?, ?, 0)",
                    (version, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            except Exception:
                pass
            self._log.error("migration_failed", version=version, error=str(e))
            raise MigrationError(f"Migration {version} fail: {e}") from e
        finally:
            conn.close()

    @classmethod
    def from_config(cls, loader) -> MigrationRunner:
        return cls(
            db_path=loader.get("system", "paths.db_file", "data/mai.db"),
            migrations_dir="migrations",
            backup_dir=loader.get("system", "paths.backup_dir", "backups"),
        )
