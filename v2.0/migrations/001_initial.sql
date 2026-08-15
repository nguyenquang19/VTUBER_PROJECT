-- migrations/001_initial.sql
-- Phase 0: schema nền tảng cho logging turn + state + trigger.
--
-- Nguyên tắc 8.8.4: migration chỉ THÊM, không SỬA/XOÁ. Dùng IF NOT EXISTS
-- để idempotent (chạy lại không lỗi). schema_migrations do runner tạo trước.

-- Turn hội thoại (nguồn truy vấn; JSONL ở logs/turns.jsonl là log thô song song).
CREATE TABLE IF NOT EXISTS turns (
    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    trigger_type TEXT,
    trigger_source TEXT,
    user_name TEXT,
    input_content TEXT,
    raw_output TEXT,
    parsed_text TEXT,
    mood_json TEXT,
    interrupted INTEGER DEFAULT 0,
    ttfa_ms INTEGER,
    total_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);

-- State machine transitions (ARCHITECTURE 9.2)
CREATE TABLE IF NOT EXISTS state_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    trigger_type TEXT,
    duration_in_prev_state_ms INTEGER,
    turn_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_state_timestamp ON state_transitions(timestamp);

-- Trigger decisions (ARCHITECTURE 9.2)
CREATE TABLE IF NOT EXISTS trigger_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    event_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    action TEXT NOT NULL,           -- respond / queue / skip
    priority INTEGER,
    reason TEXT,
    queue_position INTEGER,
    processed_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_trigger_timestamp ON trigger_decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_trigger_action ON trigger_decisions(action);
