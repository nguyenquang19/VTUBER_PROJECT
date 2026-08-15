-- M7.4: grounded positive interaction evidence for operator-reviewed running gags.
CREATE TABLE IF NOT EXISTS relationship_positive_events (
    event_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    occurred_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relationship_positive_viewer
    ON relationship_positive_events(viewer_id, occurred_at);

