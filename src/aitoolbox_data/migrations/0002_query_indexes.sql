CREATE INDEX request_project_time ON requests(project_id,created_at_ns,request_id);
CREATE INDEX request_project_model_time ON requests(project_id,model,created_at_ns,request_id);
CREATE INDEX request_project_batch_time ON requests(project_id,batch_id,created_at_ns,request_id);
CREATE INDEX batches_project_object ON batches(project_id,object_id,batch_id);
CREATE INDEX request_source ON requests(source_id,source_key);
CREATE INDEX attempts_execution ON attempts(project_id,execution_id);
CREATE INDEX artifact_hash ON artifacts(project_id,sha256);
CREATE INDEX usage_request ON usage_facts(project_id,request_id,attempt);
CREATE INDEX usage_recorded_time ON usage_facts(recorded_at_ns,project_id,request_id);
CREATE INDEX resource_time ON resource_observations(source_id,captured_at_ns,observation_id);
CREATE INDEX gaps_project ON source_gaps(project_id,request_id);
CREATE TABLE artifact_links (
  project_id TEXT NOT NULL, request_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
  role TEXT NOT NULL, created_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,request_id,artifact_id,role),
  FOREIGN KEY(project_id,request_id) REFERENCES requests(project_id,request_id),
  FOREIGN KEY(project_id,artifact_id) REFERENCES artifacts(project_id,artifact_id)
);
CREATE INDEX artifact_links_request ON artifact_links(project_id,request_id,role);
CREATE TABLE source_references (
  project_id TEXT NOT NULL, request_id TEXT NOT NULL, ref_id TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES sources(source_id), role TEXT NOT NULL,
  uri TEXT NOT NULL, observed_sha256 TEXT, observed_bytes INTEGER,
  recovery_state TEXT NOT NULL, unknown_reason TEXT,
  created_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,request_id,ref_id),
  FOREIGN KEY(project_id,request_id) REFERENCES requests(project_id,request_id),
  CHECK(observed_sha256 IS NULL OR length(observed_sha256)=64),
  CHECK(observed_bytes IS NULL OR observed_bytes>=0)
);
CREATE INDEX source_refs_request ON source_references(project_id,request_id,role);
CREATE TABLE import_cursors (
  source_id TEXT NOT NULL REFERENCES sources(source_id), project_id TEXT NOT NULL REFERENCES projects(project_id),
  stream TEXT NOT NULL, source_instance TEXT NOT NULL,
  watermark_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL,
  expected_count INTEGER NOT NULL, emitted_count INTEGER NOT NULL,
  completed INTEGER NOT NULL CHECK(completed IN (0,1)),
  last_page_sha256 TEXT NOT NULL CHECK(length(last_page_sha256)=64),
  updated_at_ns INTEGER NOT NULL,
  PRIMARY KEY(source_id,project_id,stream)
);
CREATE TABLE legacy_cloud_events (
  event_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  occurred_at TEXT NOT NULL, day_cst TEXT NOT NULL,
  caller TEXT NOT NULL, provider TEXT NOT NULL, model TEXT,
  method TEXT NOT NULL, path_sha256 TEXT NOT NULL CHECK(length(path_sha256)=64),
  status_code INTEGER, outcome TEXT NOT NULL, usage_status TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
  upstream_request_id TEXT, client_request_id TEXT,
  unknown_reason TEXT NOT NULL, imported_at_ns INTEGER NOT NULL
);
CREATE TABLE legacy_cloud_metrics (
  event_id TEXT NOT NULL REFERENCES legacy_cloud_events(event_id),
  metric_name TEXT NOT NULL, unit TEXT NOT NULL, quantity_decimal TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY(event_id,metric_name,unit)
);
CREATE INDEX legacy_usage_day ON legacy_cloud_events(day_cst,caller,provider,model,event_id);
