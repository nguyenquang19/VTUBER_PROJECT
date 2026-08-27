"""Test SqliteVecStore (Phase 7.B) — không cần embedding model, dùng vec giả."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.migration_runner import MigrationRunner
from services.memory.sqlite_vec_store import SqliteVecStore

REPO_ROOT = Path(__file__).resolve().parents[2]

DIM = 1024  # bge-m3


def fake_vec(seed: float) -> list[float]:
    """Sinh vec deterministic: mọi dimension bằng seed. Cosine distance giữa 2
    vec constant khác nhau vẫn có ordering theo độ chênh."""
    return [seed] * DIM


@pytest.fixture
def store(tmp_path: Path) -> SqliteVecStore:
    """Store trên DB đã migrate (dùng runner thật)."""
    db = tmp_path / "mai.db"
    MigrationRunner(db, REPO_ROOT / "migrations", tmp_path / "backups").initialize()
    s = SqliteVecStore(db_path=db)
    yield s
    s.close()


class TestInit:
    def test_load_extension_ok(self, store: SqliteVecStore) -> None:
        # nếu init không raise là đã load được sqlite-vec
        assert store.count() == 0

    def test_requires_db_or_conn(self) -> None:
        with pytest.raises(ValueError, match="db_path HOẶC conn"):
            SqliteVecStore()

    def test_accepts_injected_conn(self, tmp_path: Path) -> None:
        db = tmp_path / "mai.db"
        MigrationRunner(db, REPO_ROOT / "migrations", tmp_path / "backups").initialize()
        conn = sqlite3.connect(str(db))
        s = SqliteVecStore(conn=conn)
        assert s.count() == 0
        s.close()
        conn.close()  # store không own → user tự close


class TestInsertAndFetch:
    def test_insert_and_fetch_by_id(self, store: SqliteVecStore) -> None:
        store.insert(
            entry_id="m1",
            content="Mai thích cà phê sữa đá",
            embedding=fake_vec(0.1),
            tier="persistent",
            importance=0.7,
            tags=["preference", "food"],
            metadata={"source": "operator"},
            viewer_id="viewer_abc",
        )
        e = store.fetch_by_id("m1")
        assert e is not None
        assert e.entry_id == "m1"
        assert e.content == "Mai thích cà phê sữa đá"
        assert e.tier == "persistent"
        assert e.importance == 0.7
        assert e.tags == ["preference", "food"]
        assert e.metadata == {"source": "operator"}
        assert e.viewer_id == "viewer_abc"

    def test_fetch_nonexistent_returns_none(self, store: SqliteVecStore) -> None:
        assert store.fetch_by_id("nope") is None

    def test_insert_is_idempotent_on_conflict(self, store: SqliteVecStore) -> None:
        store.insert("m1", "v1", fake_vec(0.1))
        store.insert("m1", "v2 updated", fake_vec(0.2), importance=0.9)
        e = store.fetch_by_id("m1")
        assert e is not None
        assert e.content == "v1"
        assert e.importance == 0.5
        # idempotency key giữ bản ghi đầu tiên và không tạo duplicate vector.
        results = store.query_knn(fake_vec(0.2), top_k=5)
        ids = [r.entry_id for r in results]
        assert ids.count("m1") == 1

    def test_count(self, store: SqliteVecStore) -> None:
        assert store.count() == 0
        store.insert("m1", "a", fake_vec(0.1), tier="persistent")
        store.insert("m2", "b", fake_vec(0.2), tier="session")
        store.insert("m3", "c", fake_vec(0.3), tier="persistent")
        assert store.count() == 3
        assert store.count(tier="persistent") == 2
        assert store.count(tier="session") == 1


class TestQueryKnn:
    def test_returns_sorted_by_distance(self, store: SqliteVecStore) -> None:
        store.insert("far", "far", fake_vec(1.0))
        store.insert("near", "near", fake_vec(0.11))
        store.insert("mid", "mid", fake_vec(0.5))
        results = store.query_knn(fake_vec(0.1), top_k=3)
        assert [r.entry_id for r in results] == ["near", "mid", "far"]
        # distance tăng dần
        assert results[0].distance <= results[1].distance <= results[2].distance

    def test_top_k_limits(self, store: SqliteVecStore) -> None:
        for i in range(5):
            store.insert(f"m{i}", f"c{i}", fake_vec(0.1 * i))
        results = store.query_knn(fake_vec(0.0), top_k=2)
        assert len(results) == 2

    def test_filter_by_tier(self, store: SqliteVecStore) -> None:
        store.insert("p1", "p1", fake_vec(0.1), tier="persistent")
        store.insert("s1", "s1", fake_vec(0.15), tier="session")
        store.insert("p2", "p2", fake_vec(0.2), tier="persistent")
        results = store.query_knn(fake_vec(0.1), top_k=5, tier="persistent")
        ids = {r.entry_id for r in results}
        assert ids == {"p1", "p2"}

    def test_filter_by_viewer(self, store: SqliteVecStore) -> None:
        store.insert("a1", "a1", fake_vec(0.1), viewer_id="viewer_a")
        store.insert("b1", "b1", fake_vec(0.15), viewer_id="viewer_b")
        store.insert("a2", "a2", fake_vec(0.2), viewer_id="viewer_a")
        results = store.query_knn(fake_vec(0.1), top_k=5, viewer_id="viewer_a")
        ids = {r.entry_id for r in results}
        assert ids == {"a1", "a2"}

    def test_empty_store_returns_empty(self, store: SqliteVecStore) -> None:
        assert store.query_knn(fake_vec(0.1), top_k=3) == []


class TestDelete:
    def test_delete_removes_from_both_tables(self, store: SqliteVecStore) -> None:
        store.insert("m1", "hi", fake_vec(0.1))
        assert store.delete("m1") is True
        assert store.fetch_by_id("m1") is None
        # cũng phải xoá khỏi vec table
        assert store.query_knn(fake_vec(0.1), top_k=5) == []

    def test_delete_nonexistent_returns_false(self, store: SqliteVecStore) -> None:
        assert store.delete("nope") is False

    def test_evict_oldest_excess_bounds_both_tables(self, store: SqliteVecStore) -> None:
        now = datetime.now(timezone.utc)
        store.insert("old", "old", fake_vec(0.1), timestamp=now - timedelta(days=2))
        store.insert("mid", "mid", fake_vec(0.2), timestamp=now - timedelta(days=1))
        store.insert("new", "new", fake_vec(0.3), timestamp=now)
        assert store.evict_oldest_excess(2) == 1
        assert store.count() == 2
        assert store.fetch_by_id("old") is None
        assert {item.entry_id for item in store.query_knn(fake_vec(0.2), top_k=5)} == {
            "mid", "new",
        }

    @pytest.mark.parametrize("value", [0, True, 1.5])
    def test_evict_bound_is_strict(self, store: SqliteVecStore, value: object) -> None:
        with pytest.raises(ValueError):
            store.evict_oldest_excess(value)  # type: ignore[arg-type]
