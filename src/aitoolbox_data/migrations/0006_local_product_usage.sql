-- V10 local product receipts remain the source of truth until its ingress
-- uses the catalog contract. These rows are a hash-checked read projection.
CREATE TABLE local_product_calls (
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  request_id TEXT NOT NULL,
  model TEXT NOT NULL,
  model_revision TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING','COMPLETED','FAILED','UNKNOWN')),
  created_at_ns INTEGER NOT NULL,
  updated_at_ns INTEGER NOT NULL,
  input_tokens INTEGER CHECK(input_tokens >= 0),
  output_tokens INTEGER CHECK(output_tokens >= 0),
  usage_unknown_reason TEXT,
  source_uri TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  source_bytes INTEGER NOT NULL CHECK(source_bytes >= 0),
  imported_at_ns INTEGER NOT NULL,
  PRIMARY KEY(source_id,request_id),
  CHECK((input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND usage_unknown_reason IS NULL)
     OR ((input_tokens IS NULL OR output_tokens IS NULL) AND usage_unknown_reason IS NOT NULL))
);
CREATE INDEX local_product_calls_day ON local_product_calls(created_at_ns,model,request_id);
CREATE TABLE local_product_call_versions (
  source_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  state TEXT NOT NULL,
  updated_at_ns INTEGER NOT NULL,
  observed_at_ns INTEGER NOT NULL,
  PRIMARY KEY(source_id,request_id,source_sha256),
  FOREIGN KEY(source_id,request_id) REFERENCES local_product_calls(source_id,request_id)
);
