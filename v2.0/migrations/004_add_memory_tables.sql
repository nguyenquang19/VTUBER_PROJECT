-- migrations/004_add_memory_tables.sql
-- Phase 7: Memory system (ARCHITECTURE 8.8 + 11.8)
--
-- Nguyên tắc 8.8.4: chỉ THÊM, không SỬA/XOÁ. IF NOT EXISTS để idempotent.
-- Runner phải load sqlite-vec extension trước khi apply (đã fix trong
-- migration_runner._try_load_sqlite_vec).

-- Bảng chính: metadata memory entries. Cả 3 tier (WORKING/SESSION/PERSISTENT)
-- lưu chung, phân biệt qua cột `tier`. WORKING thường không lưu DB (in-memory
-- deque, Phase 7.E) — chỉ SESSION/PERSISTENT thực sự persist.
CREATE TABLE IF NOT EXISTS memory_entries (
    entry_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    tier TEXT NOT NULL,               -- 'working' | 'session' | 'persistent'
    importance REAL DEFAULT 0.5,      -- 0.0-1.0, dùng để rank khi query
    tags_json TEXT,                   -- JSON array string
    metadata_json TEXT,               -- JSON object string
    viewer_id TEXT,                   -- multi-viewer profile (Phase 7.F); NULL = system/operator
    session_id TEXT                   -- SESSION tier gom theo session (Phase 7.F)
);

CREATE INDEX IF NOT EXISTS idx_memory_timestamp ON memory_entries(timestamp);
CREATE INDEX IF NOT EXISTS idx_memory_tier ON memory_entries(tier);
CREATE INDEX IF NOT EXISTS idx_memory_viewer ON memory_entries(viewer_id);
CREATE INDEX IF NOT EXISTS idx_memory_session ON memory_entries(session_id);

-- Vector index cho semantic search. bge-m3 embedding dim = 1024.
-- vec0 là virtual table của sqlite-vec (extension loaded bởi runner).
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
    entry_id TEXT PRIMARY KEY,
    embedding float[1024]
);
