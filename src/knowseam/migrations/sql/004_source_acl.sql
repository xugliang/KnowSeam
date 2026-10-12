CREATE SEQUENCE sk_permission_sequence;
CREATE TABLE sk_source_policies (
 source_id text PRIMARY KEY REFERENCES sk_sources(id),
 mode text NOT NULL CHECK(mode IN ('public_verified','source_acl')), config_hash text NOT NULL
);
CREATE TABLE sk_document_permissions (
 document_id text PRIMARY KEY REFERENCES sk_documents(id),
 observation_sequence bigint NOT NULL, revision text NOT NULL, config_hash text NOT NULL,
 state text NOT NULL CHECK(state IN('verified','unknown','revoked')),
 public boolean NOT NULL, complete boolean NOT NULL, verified_at timestamptz,
 CHECK(NOT public OR (state='verified' AND complete))
);
CREATE TABLE sk_document_grants (
 document_id text NOT NULL REFERENCES sk_document_permissions(document_id),
 kind text NOT NULL CHECK(kind IN('user','group')), subject_id text NOT NULL,
 PRIMARY KEY(document_id,kind,subject_id)
);
CREATE TABLE sk_source_bindings (
 source_id text NOT NULL REFERENCES sk_source_policies(source_id), company_id text NOT NULL,
 actor_id text NOT NULL, kind text NOT NULL CHECK(kind IN('user','group')),
 subject_id text NOT NULL, valid_until timestamptz NOT NULL,
 PRIMARY KEY(source_id,actor_id,kind,subject_id),
 FOREIGN KEY(company_id,actor_id) REFERENCES sk_employees(company_id,actor_id)
);
CREATE INDEX sk_source_bindings_lookup ON sk_source_bindings(company_id,actor_id,source_id,kind,subject_id);
