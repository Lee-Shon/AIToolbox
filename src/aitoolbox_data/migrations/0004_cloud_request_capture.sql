-- New cloud admissions commit the private request snapshot and optional chunk wire
-- as part of the same transaction as the durable request identity.
CREATE TABLE cloud_request_capture (
  project_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  snapshot_artifact_id TEXT NOT NULL,
  wire_artifact_id TEXT,
  created_at_ns INTEGER NOT NULL,
  PRIMARY KEY(project_id,request_id),
  FOREIGN KEY(project_id,request_id) REFERENCES requests(project_id,request_id),
  FOREIGN KEY(project_id,snapshot_artifact_id) REFERENCES artifacts(project_id,artifact_id),
  FOREIGN KEY(project_id,wire_artifact_id) REFERENCES artifacts(project_id,artifact_id)
);
