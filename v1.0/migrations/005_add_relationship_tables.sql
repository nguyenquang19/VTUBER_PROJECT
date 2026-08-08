-- M7: pseudonymous viewer profiles and grounded social/narrative records.
CREATE TABLE IF NOT EXISTS viewer_profiles (
    viewer_id TEXT PRIMARY KEY,
    interaction_count INTEGER NOT NULL,
    first_seen DATETIME NOT NULL,
    last_seen DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    confirmed_preferences_json TEXT NOT NULL DEFAULT '[]',
    boundaries_json TEXT NOT NULL DEFAULT '[]',
    tone TEXT,
    updated_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_viewer_profiles_expiry ON viewer_profiles(expires_at);

CREATE TABLE IF NOT EXISTS relationship_seen_events (
    event_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    occurred_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relationship_seen_viewer ON relationship_seen_events(viewer_id);
CREATE INDEX IF NOT EXISTS idx_relationship_seen_time ON relationship_seen_events(occurred_at);

CREATE TABLE IF NOT EXISTS relationship_notes (
    note_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    reviewed_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_relationship_notes_viewer ON relationship_notes(viewer_id);

CREATE TABLE IF NOT EXISTS narrative_items (
    narrative_id TEXT PRIMARY KEY,
    viewer_id TEXT,
    summary TEXT NOT NULL,
    event_refs_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_narrative_status ON narrative_items(status, expires_at);

CREATE TABLE IF NOT EXISTS running_gags (
    gag_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    event_refs_json TEXT NOT NULL,
    status TEXT NOT NULL,
    positive_count INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    last_referenced_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_running_gags_viewer ON running_gags(viewer_id, status);

CREATE TABLE IF NOT EXISTS relationship_audit (
    audit_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    viewer_id TEXT,
    target_id TEXT,
    reason TEXT NOT NULL,
    created_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relationship_audit_time ON relationship_audit(created_at);

