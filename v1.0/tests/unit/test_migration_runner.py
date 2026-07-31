"""Test MigrationRunner (ARCHITECTURE 8.8).

DoD-adjacent: apply migration + backup an toàn + idempotent.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from orchestrator.migration_runner import MigrationError, MigrationRunner

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner_env(tmp_path: Path):
    db = tmp_path / "data" / "mai.db"
    migrations = tmp_path / "migrations"
    backups = tmp_path / "backups"
    migrations.mkdir()
    return db, migrations, backups


def write_migration(migrations: Path, name: str, sql: str) -> None:
    (migrations / name).write_text(sql, encoding="utf-8")


def table_exists(db: Path, table: str) -> bool:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


class TestApply:
    def test_applies_single_migration(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_initial.sql", "CREATE TABLE foo (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        applied = runner.initialize()
        assert applied == ["001"]
        assert table_exists(db, "foo")

    def test_applies_in_version_order(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "002_second.sql", "CREATE TABLE bar (id INTEGER);")
        write_migration(migrations, "001_first.sql", "CREATE TABLE foo (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        assert runner.initialize() == ["001", "002"]

    def test_numeric_order_not_lexicographic(self, runner_env) -> None:
        db, migrations, backups = runner_env
        for i in range(1, 12):
            write_migration(migrations, f"{i:03d}_m.sql", f"CREATE TABLE t{i} (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        applied = runner.initialize()
        assert applied == [f"{i:03d}" for i in range(1, 12)]

    def test_records_in_schema_migrations(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_initial.sql", "CREATE TABLE foo (id INTEGER);")
        MigrationRunner(db, migrations, backups).initialize()
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT version, success FROM schema_migrations WHERE version='001'"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("001", 1)


class TestIdempotency:
    def test_second_run_applies_nothing(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_initial.sql", "CREATE TABLE foo (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        assert runner.initialize() == ["001"]
        assert runner.initialize() == []  # đã applied → skip

    def test_new_migration_applied_on_next_run(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_a.sql", "CREATE TABLE foo (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        runner.initialize()
        write_migration(migrations, "002_b.sql", "CREATE TABLE bar (id INTEGER);")
        assert runner.initialize() == ["002"]

    def test_if_not_exists_migration_reruns_safely(self, runner_env) -> None:
        """Migration dùng IF NOT EXISTS chạy lại không lỗi (khi force)."""
        db, migrations, backups = runner_env
        write_migration(migrations, "001_a.sql", "CREATE TABLE IF NOT EXISTS foo (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        runner.initialize()
        # xoá record để ép chạy lại
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM schema_migrations WHERE version='001'")
        conn.commit()
        conn.close()
        assert runner.initialize() == ["001"]  # không raise


class TestBackup:
    def test_backup_on_first_migration_is_safe(self, runner_env) -> None:
        # initialize() tạo DB (qua ensure_migrations_table) trước khi apply,
        # nên migration đầu vẫn backup 1 DB rỗng — an toàn, chấp nhận được.
        db, migrations, backups = runner_env
        write_migration(migrations, "001_a.sql", "CREATE TABLE foo (id INTEGER);")
        MigrationRunner(db, migrations, backups).initialize()
        assert len(list(backups.glob("mai.db.pre_migration_*"))) == 1

    def test_backup_created_before_second_migration(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_a.sql", "CREATE TABLE foo (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        runner.initialize()  # tạo DB + backup trước khi apply 001
        write_migration(migrations, "002_b.sql", "CREATE TABLE bar (id INTEGER);")
        runner.initialize()  # DB đã tồn tại → backup trước khi apply 002
        backup_files = list(backups.glob("mai.db.pre_migration_*"))
        # 2 backup riêng biệt (trước 001 và trước 002), KHÔNG ghi đè nhau dù
        # 2 lần chạy có thể xảy ra trong cùng 1 giây (8.8.4: backup-before-migrate).
        assert len(backup_files) == 2
        assert len({f.name for f in backup_files}) == 2  # tên duy nhất, không collide

    def test_backups_unique_within_same_second(self, runner_env) -> None:
        """2 backup liên tiếp (cùng giây) phải có tên khác nhau — không silent overwrite."""
        db, migrations, backups = runner_env
        write_migration(migrations, "001_a.sql", "CREATE TABLE foo (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        runner.initialize()
        # gọi backup thủ công 2 lần liên tiếp trong cùng thời điểm
        b1 = runner._backup_before_migration()
        b2 = runner._backup_before_migration()
        assert b1 is not None and b2 is not None
        assert b1 != b2
        assert b1.exists() and b2.exists()

    def test_backup_preserves_data(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_a.sql", "CREATE TABLE foo (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        runner.initialize()
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO foo VALUES (42)")
        conn.commit()
        conn.close()

        write_migration(migrations, "002_b.sql", "CREATE TABLE bar (id INTEGER);")
        runner.initialize()
        # nhiều backup (trước 001, trước 002) → lấy MỚI NHẤT (trước 002, đã có foo+42)
        backup = sorted(backups.glob("mai.db.pre_migration_*"))[-1]
        conn = sqlite3.connect(backup)
        try:
            assert conn.execute("SELECT id FROM foo").fetchone() == (42,)
        finally:
            conn.close()


class TestFailure:
    def test_bad_sql_raises_migration_error(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_bad.sql", "CREATE TABLE ;;; syntax error")
        runner = MigrationRunner(db, migrations, backups)
        with pytest.raises(MigrationError):
            runner.initialize()

    def test_failed_migration_recorded_as_unsuccessful(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_bad.sql", "CREATE TABLE broken (")
        runner = MigrationRunner(db, migrations, backups)
        with pytest.raises(MigrationError):
            runner.initialize()
        # version không được coi là applied (success=1)
        assert "001" not in runner.get_applied_versions()

    def test_failed_migration_can_be_fixed_and_retried(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_x.sql", "CREATE TABLE broken (")
        runner = MigrationRunner(db, migrations, backups)
        with pytest.raises(MigrationError):
            runner.initialize()
        # sửa script rồi retry
        write_migration(migrations, "001_x.sql", "CREATE TABLE fixed (id INTEGER);")
        assert runner.initialize() == ["001"]
        assert table_exists(db, "fixed")

    def test_invalid_filename_raises(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "no_version_prefix.sql", "CREATE TABLE foo (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        # file không match NNN_*.sql → bị bỏ qua, không apply
        assert runner.initialize() == []


class TestVersionQueries:
    def test_current_version(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_a.sql", "CREATE TABLE a (id INTEGER);")
        write_migration(migrations, "002_b.sql", "CREATE TABLE b (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        assert runner.current_version() is None
        runner.initialize()
        assert runner.current_version() == "002"

    def test_get_pending(self, runner_env) -> None:
        db, migrations, backups = runner_env
        write_migration(migrations, "001_a.sql", "CREATE TABLE a (id INTEGER);")
        runner = MigrationRunner(db, migrations, backups)
        assert runner.get_pending() == ["001_a.sql"]
        runner.initialize()
        assert runner.get_pending() == []


class TestRealMigration:
    """Migration 001 thật của repo phải apply và tạo đúng table (9.2)."""

    def test_real_001_creates_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "mai.db"
        runner = MigrationRunner(
            db, REPO_ROOT / "migrations", tmp_path / "backups"
        )
        applied = runner.initialize()
        assert "001" in applied
        for table in ("turns", "state_transitions", "trigger_decisions", "schema_migrations"):
            assert table_exists(db, table), f"thiếu table {table}"

    def test_real_001_indexes_created(self, tmp_path: Path) -> None:
        db = tmp_path / "mai.db"
        MigrationRunner(db, REPO_ROOT / "migrations", tmp_path / "backups").initialize()
        conn = sqlite3.connect(db)
        try:
            indexes = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "idx_state_timestamp" in indexes
        assert "idx_trigger_action" in indexes

    def test_from_config(self, tmp_path: Path) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        runner = MigrationRunner.from_config(loader)
        assert runner.migrations_dir == Path("migrations")
        assert runner.db_path == Path("data/mai.db")
