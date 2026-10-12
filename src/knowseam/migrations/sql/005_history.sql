-- No backfill: created_at of an old row is not proof of past acceptance.
CREATE TABLE sk_version_metadata (
 version_id text PRIMARY KEY REFERENCES sk_versions(id),
 source_id text NOT NULL REFERENCES sk_sources(id), config_hash text NOT NULL,
 title text NOT NULL, source_url text NOT NULL, accepted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE sk_fact_observations (
 id text PRIMARY KEY, company_id text NOT NULL, collection_id text NOT NULL REFERENCES sk_collections(id),
 subject text NOT NULL, subject_type text NOT NULL, subject_id text NOT NULL, object_id text NOT NULL,
 relation text NOT NULL, object text NOT NULL,
 object_type text NOT NULL, qualifier text NOT NULL DEFAULT '',
 valid_from timestamptz, valid_until timestamptz,
 recorded_from timestamptz NOT NULL DEFAULT clock_timestamp(), recorded_until timestamptz,
 supersedes text REFERENCES sk_fact_observations(id),
 CHECK(valid_from IS NULL OR valid_until IS NULL OR valid_from<valid_until),
 CHECK(recorded_until IS NULL OR recorded_until>recorded_from)
);
CREATE TABLE sk_fact_supports (
 observation_id text NOT NULL REFERENCES sk_fact_observations(id),
 version_id text NOT NULL, chunk_id text NOT NULL, quote text NOT NULL,
 recorded_from timestamptz NOT NULL DEFAULT clock_timestamp(), recorded_until timestamptz,
 PRIMARY KEY(observation_id,version_id,chunk_id),
 FOREIGN KEY(version_id,chunk_id) REFERENCES sk_chunks(version_id,chunk_id),
 CHECK(recorded_until IS NULL OR recorded_until>recorded_from)
);
CREATE TABLE sk_fact_events (
 event_key text PRIMARY KEY, company_id text NOT NULL, document_id text NOT NULL REFERENCES sk_documents(id),
 version_id text REFERENCES sk_versions(id), kind text NOT NULL CHECK(kind IN('accept','correct')),
 payload_hash text NOT NULL, accepted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX sk_facts_scope_time ON sk_fact_observations(company_id,collection_id,recorded_from);
CREATE INDEX sk_facts_subject ON sk_fact_observations(company_id,collection_id,subject_id);
CREATE INDEX sk_facts_object ON sk_fact_observations(company_id,collection_id,object_id);
CREATE INDEX sk_fact_supports_version ON sk_fact_supports(version_id,chunk_id,observation_id);
