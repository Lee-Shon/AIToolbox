-- V9 intake observations are historical source facts, not new native executions.
CREATE TABLE local_history_records (
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  source_sequence INTEGER NOT NULL CHECK(source_sequence>=0),
  request_id TEXT NOT NULL,
  record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64),
  object_id TEXT, batch_key TEXT, external_batch_id TEXT,
  manifest_sha256 TEXT CHECK(manifest_sha256 IS NULL OR length(manifest_sha256)=64),
  batch_created_ns INTEGER,
  request_fingerprint TEXT CHECK(request_fingerprint IS NULL OR length(request_fingerprint)=64),
  fingerprint_source TEXT, model TEXT, binding_revision TEXT,
  config_snapshot_sha256 TEXT, config_unknown_reason TEXT,
  config_id TEXT, execution_id TEXT, attempt INTEGER,
  original_state TEXT, delivery TEXT, observed_receipt_state TEXT,
  native_usage_json TEXT, source_references_json TEXT, unknown_json TEXT NOT NULL,
  core_request_registered INTEGER NOT NULL CHECK(core_request_registered IN (0,1)),
  imported_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,source_sequence), UNIQUE(project_id,request_id)
);
CREATE INDEX local_history_request ON local_history_records(project_id,request_id);
CREATE INDEX local_history_execution ON local_history_records(project_id,execution_id);
CREATE TABLE local_history_refs (
  project_id TEXT NOT NULL, source_sequence INTEGER NOT NULL,
  ref_id TEXT NOT NULL, role TEXT NOT NULL, uri TEXT NOT NULL,
  observed_sha256 TEXT CHECK(observed_sha256 IS NULL OR length(observed_sha256)=64),
  observed_bytes INTEGER CHECK(observed_bytes IS NULL OR observed_bytes>=0),
  declared_sha256 TEXT, declared_bytes INTEGER,
  recovery_state TEXT NOT NULL, unknown_reason TEXT,
  PRIMARY KEY(project_id,source_sequence,ref_id),
  FOREIGN KEY(project_id,source_sequence) REFERENCES local_history_records(project_id,source_sequence)
);
CREATE INDEX local_history_refs_role ON local_history_refs(project_id,source_sequence,role);
CREATE TABLE local_history_usage (
  project_id TEXT NOT NULL, source_sequence INTEGER NOT NULL,
  metric_name TEXT NOT NULL, unit TEXT NOT NULL,
  quantity_decimal TEXT, knowledge TEXT NOT NULL CHECK(knowledge IN ('known','unknown')),
  unknown_reason TEXT, provenance TEXT,
  PRIMARY KEY(project_id,source_sequence,metric_name,unit),
  FOREIGN KEY(project_id,source_sequence) REFERENCES local_history_records(project_id,source_sequence),
  CHECK((knowledge='known' AND quantity_decimal IS NOT NULL AND unknown_reason IS NULL)
     OR (knowledge='unknown' AND quantity_decimal IS NULL AND unknown_reason IS NOT NULL))
);
CREATE TABLE local_history_events (
  project_id TEXT NOT NULL REFERENCES projects(project_id),
  event_sequence INTEGER NOT NULL CHECK(event_sequence>=0),
  request_id TEXT NOT NULL, request_source_sequence INTEGER NOT NULL,
  event_key TEXT NOT NULL, state TEXT NOT NULL, occurred_at_ns INTEGER,
  reason_sha256 TEXT CHECK(reason_sha256 IS NULL OR length(reason_sha256)=64),
  reason_bytes INTEGER CHECK(reason_bytes IS NULL OR reason_bytes>=0),
  reason_uri TEXT, evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('request_preview','events_page')),
  unknown_json TEXT NOT NULL, imported_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,event_sequence), UNIQUE(event_key)
);
CREATE INDEX local_history_events_request ON local_history_events(project_id,request_id,event_sequence);
