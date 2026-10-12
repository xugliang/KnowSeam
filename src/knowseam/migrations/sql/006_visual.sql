CREATE TABLE sk_visual_sources (
 version_id text PRIMARY KEY REFERENCES sk_versions(id), requests jsonb NOT NULL,
 CHECK(jsonb_array_length(requests) BETWEEN 1 AND 500));
CREATE TABLE sk_visual_jobs (
 id bigserial PRIMARY KEY, company_id text NOT NULL, document_id text NOT NULL REFERENCES sk_documents(id),
 version_id text NOT NULL REFERENCES sk_versions(id), fingerprint text NOT NULL, config_hash text NOT NULL,
 permission_revision text NOT NULL, requests jsonb NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','working','retry','done','obsolete','failed')),
 attempts integer NOT NULL DEFAULT 0, claim_token text, lease_until timestamptz,
 available_at timestamptz NOT NULL DEFAULT now(), error_code text,
 UNIQUE(version_id,fingerprint,config_hash,permission_revision));
CREATE INDEX sk_visual_jobs_claim ON sk_visual_jobs(company_id,state,available_at,id);
CREATE TABLE sk_visual_assets (
 id text PRIMARY KEY, version_id text NOT NULL REFERENCES sk_versions(id), fingerprint text NOT NULL,
 permission_revision text NOT NULL, config_hash text NOT NULL,
 sha256 text NOT NULL, width integer NOT NULL CHECK(width BETWEEN 1 AND 2048),
 height integer NOT NULL CHECK(height BETWEEN 1 AND 2048), locator jsonb NOT NULL,
 data bytea NOT NULL CHECK(octet_length(data) BETWEEN 1 AND 10485760),
 created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX sk_visual_assets_version ON sk_visual_assets(version_id,fingerprint);
CREATE TABLE sk_visual_vectors (
 asset_id text PRIMARY KEY REFERENCES sk_visual_assets(id), dimensions integer NOT NULL,
 token_count integer NOT NULL CHECK(token_count BETWEEN 1 AND 2048), coarse vector NOT NULL,
 payload bytea NOT NULL CHECK(octet_length(payload) BETWEEN 1 AND 1048576));
