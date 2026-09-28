-- Read-only projection of V9 Ray resource authority; this table grants nothing.
CREATE TABLE resource_source_facts (
  source_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('events','leases','resource_predictions','policy')),
  source_key TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  content_bytes INTEGER NOT NULL CHECK(content_bytes >= 0),
  source_uri TEXT NOT NULL,
  source_order_key INTEGER,
  source_rowid INTEGER,
  project_id TEXT,
  request_id TEXT,
  authority TEXT NOT NULL,
  observation_json TEXT NOT NULL,
  unknown_json TEXT NOT NULL,
  observed_at_ns INTEGER NOT NULL,
  PRIMARY KEY(source_id,source_kind,source_key,content_sha256),
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);
CREATE INDEX resource_fact_source_order ON resource_source_facts(source_id,source_kind,source_order_key,source_key);
CREATE INDEX resource_fact_request ON resource_source_facts(project_id,request_id,source_kind,source_order_key);
CREATE TABLE resource_import_cursors (
  source_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  cursor_json TEXT,
  watermark_json TEXT NOT NULL,
  reconciliation_json TEXT NOT NULL,
  snapshot_semantics TEXT NOT NULL,
  last_order_key INTEGER,
  complete INTEGER NOT NULL CHECK(complete IN (0,1)),
  updated_at_ns INTEGER NOT NULL,
  PRIMARY KEY(source_id,source_kind),
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);
