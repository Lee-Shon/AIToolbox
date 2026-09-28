CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL,
  applied_at_ns INTEGER NOT NULL
);
CREATE TABLE projects (
  project_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, created_at_ns INTEGER NOT NULL
);
CREATE TABLE callers (
  caller_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
  credential_ref TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
  created_at_ns INTEGER NOT NULL,
  UNIQUE(project_id,caller_id)
);
CREATE TABLE sources (
  source_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, authority TEXT NOT NULL,
  location TEXT NOT NULL, access_contract TEXT NOT NULL, created_at_ns INTEGER NOT NULL
);
CREATE TABLE artifacts (
  project_id TEXT NOT NULL REFERENCES projects(project_id), artifact_id TEXT NOT NULL,
  role TEXT NOT NULL, media_type TEXT NOT NULL, uri TEXT NOT NULL,
  sha256 TEXT NOT NULL CHECK(length(sha256)=64), bytes INTEGER NOT NULL CHECK(bytes>=0),
  source_id TEXT REFERENCES sources(source_id), recovery_state TEXT NOT NULL,
  created_at_ns INTEGER NOT NULL, PRIMARY KEY(project_id,artifact_id)
);
CREATE TABLE config_revisions (
  project_id TEXT NOT NULL REFERENCES projects(project_id), config_id TEXT NOT NULL,
  model TEXT NOT NULL, software_version TEXT NOT NULL, revision TEXT NOT NULL,
  sha256 TEXT CHECK(sha256 IS NULL OR length(sha256)=64),
  knowledge TEXT NOT NULL CHECK(knowledge IN ('known','unknown')),
  unknown_reason TEXT,
  snapshot_artifact_id TEXT, source_id TEXT REFERENCES sources(source_id),
  created_at_ns INTEGER NOT NULL, PRIMARY KEY(project_id,config_id),
  CHECK((knowledge='known' AND sha256 IS NOT NULL AND unknown_reason IS NULL)
     OR (knowledge='unknown' AND sha256 IS NULL AND unknown_reason IS NOT NULL)),
  FOREIGN KEY(project_id,snapshot_artifact_id) REFERENCES artifacts(project_id,artifact_id)
);
CREATE TABLE objects (
  project_id TEXT NOT NULL REFERENCES projects(project_id), object_id TEXT NOT NULL,
  source_id TEXT REFERENCES sources(source_id), created_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,object_id)
);
CREATE TABLE batches (
  project_id TEXT NOT NULL, batch_id TEXT NOT NULL, external_batch_id TEXT NOT NULL,
  object_id TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  source_id TEXT REFERENCES sources(source_id), source_key TEXT, created_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,batch_id),
  FOREIGN KEY(project_id,object_id) REFERENCES objects(project_id,object_id),
  UNIQUE(source_id,source_key)
);
CREATE TABLE requests (
  project_id TEXT NOT NULL, request_id TEXT NOT NULL, batch_id TEXT NOT NULL,
  caller_id TEXT, model TEXT NOT NULL, config_id TEXT NOT NULL,
  input_artifact_id TEXT, request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint)=64),
  provider TEXT, method TEXT, route_sha256 TEXT,
  source_id TEXT REFERENCES sources(source_id), source_key TEXT,
  state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
  created_at_ns INTEGER NOT NULL, updated_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,request_id),
  FOREIGN KEY(project_id,batch_id) REFERENCES batches(project_id,batch_id),
  FOREIGN KEY(project_id,caller_id) REFERENCES callers(project_id,caller_id),
  FOREIGN KEY(project_id,config_id) REFERENCES config_revisions(project_id,config_id),
  FOREIGN KEY(project_id,input_artifact_id) REFERENCES artifacts(project_id,artifact_id),
  UNIQUE(source_id,source_key)
);
CREATE TABLE attempts (
  project_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL CHECK(attempt>0),
  execution_id TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
  receipt_artifact_id TEXT, stop_proof_artifact_id TEXT, result_artifact_id TEXT,
  partial_artifact_id TEXT, error_code TEXT, status_code INTEGER,
  upstream_request_id TEXT, outcome TEXT, usage_status TEXT,
  created_at_ns INTEGER NOT NULL, updated_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,request_id,attempt),
  UNIQUE(project_id,execution_id),
  FOREIGN KEY(project_id,request_id) REFERENCES requests(project_id,request_id),
  FOREIGN KEY(project_id,receipt_artifact_id) REFERENCES artifacts(project_id,artifact_id),
  FOREIGN KEY(project_id,stop_proof_artifact_id) REFERENCES artifacts(project_id,artifact_id),
  FOREIGN KEY(project_id,result_artifact_id) REFERENCES artifacts(project_id,artifact_id),
  FOREIGN KEY(project_id,partial_artifact_id) REFERENCES artifacts(project_id,artifact_id)
);
CREATE TABLE transitions (
  project_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  event_key TEXT NOT NULL, state TEXT NOT NULL, error_code TEXT,
  source_id TEXT REFERENCES sources(source_id), occurred_at_ns INTEGER,
  recorded_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,request_id,attempt,event_key),
  FOREIGN KEY(project_id,request_id,attempt) REFERENCES attempts(project_id,request_id,attempt)
);
CREATE TABLE usage_facts (
  project_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  source_id TEXT NOT NULL REFERENCES sources(source_id), source_event_key TEXT NOT NULL,
  metric_name TEXT NOT NULL, unit TEXT NOT NULL, quantity_decimal TEXT,
  knowledge TEXT NOT NULL CHECK(knowledge IN ('known','unknown')),
  unknown_reason TEXT, provenance TEXT NOT NULL, recorded_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,source_id,source_event_key,metric_name,unit),
  FOREIGN KEY(project_id,request_id,attempt) REFERENCES attempts(project_id,request_id,attempt),
  CHECK((knowledge='known' AND quantity_decimal IS NOT NULL AND unknown_reason IS NULL)
     OR (knowledge='unknown' AND quantity_decimal IS NULL AND unknown_reason IS NOT NULL))
);
CREATE TABLE resource_observations (
  observation_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  project_id TEXT REFERENCES projects(project_id), model TEXT, config_id TEXT,
  metric_name TEXT NOT NULL, unit TEXT NOT NULL, quantity_decimal TEXT,
  knowledge TEXT NOT NULL CHECK(knowledge IN ('known','unknown')),
  captured_at_ns INTEGER, source_key TEXT NOT NULL, recorded_at_ns INTEGER NOT NULL,
  UNIQUE(source_id,source_key,metric_name,unit),
  FOREIGN KEY(project_id,config_id) REFERENCES config_revisions(project_id,config_id)
);
CREATE TABLE source_gaps (
  source_id TEXT NOT NULL REFERENCES sources(source_id), gap_key TEXT NOT NULL,
  project_id TEXT REFERENCES projects(project_id), request_id TEXT, reason TEXT NOT NULL,
  first_seen_at_ns INTEGER NOT NULL, resolved_at_ns INTEGER,
  PRIMARY KEY(source_id,gap_key)
);
