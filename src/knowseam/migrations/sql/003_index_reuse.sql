CREATE TABLE sk_token_cache (
 payload_id text PRIMARY KEY, company_id text NOT NULL, tokenizer_fingerprint text NOT NULL,
 input_hash text NOT NULL, token_count integer NOT NULL CHECK(token_count>=0), tokens jsonb NOT NULL,
 UNIQUE(company_id,tokenizer_fingerprint,input_hash), CHECK(jsonb_typeof(tokens)='array')
);
CREATE TABLE sk_term_payloads (
 payload_id text NOT NULL REFERENCES sk_token_cache(payload_id), term text NOT NULL, tf integer NOT NULL CHECK(tf>0),
 PRIMARY KEY(payload_id,term)
);
CREATE INDEX sk_term_payloads_lookup ON sk_term_payloads(term,payload_id) INCLUDE(tf);
CREATE TABLE sk_index_revisions (
 version_id text PRIMARY KEY REFERENCES sk_versions(id), tokenizer_fingerprint text NOT NULL,
 mode text NOT NULL CHECK(mode IN('shadow','reuse')), complete boolean NOT NULL
);
CREATE TABLE sk_term_postings (
 version_id text NOT NULL REFERENCES sk_index_revisions(version_id), chunk_id text NOT NULL,
 payload_id text NOT NULL REFERENCES sk_token_cache(payload_id), PRIMARY KEY(version_id,chunk_id),
 FOREIGN KEY(version_id,chunk_id) REFERENCES sk_chunks(version_id,chunk_id) ON DELETE CASCADE
);
CREATE TABLE sk_extraction_cache (
 cache_id text PRIMARY KEY, company_id text NOT NULL, collection_id text NOT NULL,
 input_hash text NOT NULL, semantic_fingerprint text NOT NULL, extraction jsonb NOT NULL,
 UNIQUE(company_id,collection_id,input_hash,semantic_fingerprint)
);
CREATE INDEX sk_term_postings_payload ON sk_term_postings(payload_id,version_id,chunk_id);
CREATE TABLE sk_derivation_dependencies (
 cache_id text NOT NULL REFERENCES sk_extraction_cache(cache_id), version_id text NOT NULL, chunk_id text NOT NULL,
 PRIMARY KEY(cache_id,version_id,chunk_id),
 FOREIGN KEY(version_id,chunk_id) REFERENCES sk_chunks(version_id,chunk_id) ON DELETE CASCADE
);
