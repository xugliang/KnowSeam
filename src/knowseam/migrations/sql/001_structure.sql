CREATE TABLE sk_sections (
 version_id text NOT NULL REFERENCES sk_versions(id), section_id text NOT NULL,
 ordinal integer NOT NULL CHECK(ordinal>=0), text text NOT NULL, heading text NOT NULL,
 locator jsonb NOT NULL, PRIMARY KEY(version_id,section_id), UNIQUE(version_id,ordinal));
CREATE TABLE sk_chunk_spans (
 version_id text NOT NULL, chunk_id text NOT NULL, section_id text NOT NULL,
 start_char integer NOT NULL CHECK(start_char>=0), end_char integer NOT NULL,
 CHECK(end_char>start_char), PRIMARY KEY(version_id,chunk_id,section_id,start_char,end_char),
 FOREIGN KEY(version_id,chunk_id) REFERENCES sk_chunks(version_id,chunk_id),
 FOREIGN KEY(version_id,section_id) REFERENCES sk_sections(version_id,section_id));
CREATE TABLE sk_catalog_items (
 collection_id text NOT NULL REFERENCES sk_collections(id), kind text NOT NULL, item_id text NOT NULL,
 name text NOT NULL, aliases jsonb NOT NULL DEFAULT '[]', metadata jsonb NOT NULL DEFAULT '{}',
 PRIMARY KEY(collection_id,kind,item_id));
CREATE TABLE sk_catalog_evidence (
 collection_id text NOT NULL, kind text NOT NULL, item_id text NOT NULL,
 version_id text NOT NULL REFERENCES sk_versions(id), chunk_id text NOT NULL,
 PRIMARY KEY(collection_id,kind,item_id,version_id,chunk_id),
 FOREIGN KEY(collection_id,kind,item_id) REFERENCES sk_catalog_items(collection_id,kind,item_id),
 FOREIGN KEY(version_id,chunk_id) REFERENCES sk_chunks(version_id,chunk_id));
CREATE INDEX sk_catalog_evidence_version ON sk_catalog_evidence(version_id);
CREATE TABLE sk_catalog_scopes (
 source_id text NOT NULL REFERENCES sk_sources(id), kind text NOT NULL,
 complete boolean NOT NULL DEFAULT false, revision text NOT NULL,
 PRIMARY KEY(source_id,kind));
