"""PostgreSQL/pgvector persistence, publication transactions and pre-retrieval ACL checks."""
from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import math
import uuid
from typing import Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .domain import AccessDenied, Chunk, NotFound, Principal, SearchHit, SourceNode


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS sk_collections (
 id text PRIMARY KEY, company_id text NOT NULL, title text NOT NULL);
CREATE TABLE IF NOT EXISTS sk_sources (
 id text PRIMARY KEY, collection_id text NOT NULL REFERENCES sk_collections(id),
 config_hash text NOT NULL, enabled boolean NOT NULL DEFAULT true, last_scan timestamptz, last_success timestamptz,
 status text NOT NULL DEFAULT 'pending', error_code text, counts jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS sk_documents (
 id text PRIMARY KEY, source_id text NOT NULL REFERENCES sk_sources(id), external_id text NOT NULL,
 title text NOT NULL, source_url text NOT NULL, source_revision text NOT NULL DEFAULT '',
 active_version text, public boolean NOT NULL DEFAULT false, acl_verified_at timestamptz,
 status text NOT NULL DEFAULT 'pending', last_seen timestamptz NOT NULL DEFAULT now(),
 error_code text, metadata jsonb NOT NULL DEFAULT '{}', UNIQUE(source_id, external_id));
CREATE TABLE IF NOT EXISTS sk_versions (
 id text PRIMARY KEY, document_id text NOT NULL REFERENCES sk_documents(id),
 raw_hash text NOT NULL, content_hash text NOT NULL, pipeline_id text NOT NULL,
 model_id text NOT NULL, dimensions integer NOT NULL, source_revision text NOT NULL,
 raw_path text NOT NULL, warnings jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS sk_chunks (
 version_id text NOT NULL REFERENCES sk_versions(id), chunk_id text NOT NULL,
 ordinal integer NOT NULL, text text NOT NULL, heading text NOT NULL,
 embedding_text text NOT NULL, content_hash text NOT NULL, locator jsonb NOT NULL,
 tokens tsvector NOT NULL, embedding vector NOT NULL, PRIMARY KEY(version_id, chunk_id));
CREATE INDEX IF NOT EXISTS sk_chunks_fts ON sk_chunks USING gin(tokens);
CREATE INDEX IF NOT EXISTS sk_chunks_order ON sk_chunks(version_id, ordinal);
CREATE INDEX IF NOT EXISTS sk_documents_source ON sk_documents(source_id);
CREATE TABLE IF NOT EXISTS sk_embedding_cache (
 model_id text NOT NULL, dimensions integer NOT NULL, input_hash text NOT NULL,
 embedding vector NOT NULL, PRIMARY KEY(model_id,dimensions,input_hash));
ALTER TABLE sk_sources ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true;
ALTER TABLE sk_collections ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'company'
 CHECK (visibility IN ('company','department','employee'));
CREATE TABLE IF NOT EXISTS sk_employees (
 company_id text NOT NULL, actor_id text NOT NULL, active boolean NOT NULL,
 PRIMARY KEY(company_id,actor_id));
CREATE TABLE IF NOT EXISTS sk_employee_departments (
 company_id text NOT NULL, actor_id text NOT NULL, department_id text NOT NULL,
 PRIMARY KEY(company_id,actor_id,department_id),
 FOREIGN KEY(company_id,actor_id) REFERENCES sk_employees(company_id,actor_id));
CREATE TABLE IF NOT EXISTS sk_collection_grants (
 collection_id text NOT NULL REFERENCES sk_collections(id),
 kind text NOT NULL CHECK (kind IN ('department','employee')), subject_id text NOT NULL,
 PRIMARY KEY(collection_id,kind,subject_id));
ALTER TABLE sk_chunks ADD COLUMN IF NOT EXISTS token_count integer CHECK (token_count >= 0);
CREATE TABLE IF NOT EXISTS sk_chunk_terms (
 version_id text NOT NULL, chunk_id text NOT NULL, term text NOT NULL, tf integer NOT NULL CHECK(tf>0),
 PRIMARY KEY(version_id,chunk_id,term),
 FOREIGN KEY(version_id,chunk_id) REFERENCES sk_chunks(version_id,chunk_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS sk_chunk_terms_lookup ON sk_chunk_terms(term,version_id,chunk_id) INCLUDE(tf);
"""


def document_id(source_id: str, external_id: str) -> str:
    return "doc-" + uuid.uuid5(uuid.NAMESPACE_URL, source_id + "\0" + external_id).hex


def vector_literal(values: Sequence[float], dimensions: int) -> str:
    if len(values) != dimensions or any(isinstance(x, bool) or not isinstance(x, (float, int))
                                       or not math.isfinite(x) for x in values):
        raise ValueError("invalid embedding dimensions or values")
    if not any(x != 0 for x in values):
        raise ValueError("zero embedding is not searchable")
    return "[" + ",".join(format(float(x), ".12g") for x in values) + "]"


class PostgresStore:
    def __init__(self, dsn: str, *, acl_ttl_seconds: int = 900):
        self._dsn = dsn
        if not 30 <= acl_ttl_seconds <= 86400:
            raise ValueError("ACL TTL must be between 30 and 86400 seconds")
        self.acl_ttl_seconds = acl_ttl_seconds

    def connect(self):
        return psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=10,
                               options="-c statement_timeout=30000 -c lock_timeout=5000")

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(SCHEMA)

    def ready(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT visibility FROM sk_collections LIMIT 1")
                connection.execute("SELECT tf FROM sk_chunk_terms LIMIT 1")
                if connection.execute("SELECT 1 FROM sk_chunks ch JOIN sk_documents d ON d.active_version=ch.version_id "
                                      "JOIN sk_sources s ON s.id=d.source_id "
                                      "WHERE s.enabled AND d.public AND ch.token_count IS NULL LIMIT 1").fetchone():
                    return False
            return True
        except psycopg.Error:
            return False

    def configure(self, *, company_id: str, collections: Sequence[dict], sources: Sequence[dict],
                  employees: Sequence[dict] = ()) -> None:
        with self.connect() as c:
            # Serialize policy replacement, including deployments with no sources.
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("acl:" + company_id,))
            existing = c.execute("SELECT s.id FROM sk_sources s JOIN sk_collections k ON k.id=s.collection_id "
                                 "WHERE k.company_id=%s", (company_id,)).fetchall()
            for source_id in sorted({r["id"] for r in existing} | {r["id"] for r in sources}):
                key = int.from_bytes(hashlib.sha256(source_id.encode()).digest()[:8], "big", signed=True)
                if not c.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (key,)).fetchone()["ok"]:
                    raise RuntimeError("source_configuration_busy_retry_after_sync")
            for item in collections:
                old = c.execute("SELECT company_id FROM sk_collections WHERE id=%s", (item["id"],)).fetchone()
                if old and old["company_id"] != company_id:
                    raise AccessDenied("collection company cannot be reassigned")
                visibility = item.get("visibility", "company")
                if visibility not in {"company", "department", "employee"}:
                    raise ValueError("invalid collection visibility")
                c.execute("INSERT INTO sk_collections(id,company_id,title,visibility) VALUES(%s,%s,%s,%s) "
                          "ON CONFLICT(id) DO UPDATE SET title=excluded.title,visibility=excluded.visibility",
                          (item["id"], company_id, item["title"], visibility))
                c.execute("DELETE FROM sk_collection_grants WHERE collection_id=%s", (item["id"],))
                for kind, field in (("department", "departments"), ("employee", "employees")):
                    for subject in set(item.get(field, ())):
                        if kind != visibility:
                            raise ValueError("grant does not match visibility")
                        c.execute("INSERT INTO sk_collection_grants VALUES(%s,%s,%s)", (item["id"], kind, subject))
            c.execute("DELETE FROM sk_employee_departments WHERE company_id=%s", (company_id,))
            c.execute("UPDATE sk_employees SET active=false WHERE company_id=%s", (company_id,))
            for employee in employees:
                c.execute("INSERT INTO sk_employees VALUES(%s,%s,%s) ON CONFLICT(company_id,actor_id) "
                          "DO UPDATE SET active=excluded.active",
                          (company_id, employee["actor_id"], employee.get("active", True)))
                for department in set(employee.get("departments", ())):
                    c.execute("INSERT INTO sk_employee_departments VALUES(%s,%s,%s)",
                              (company_id, employee["actor_id"], department))
            for item in sources:
                old = c.execute("SELECT * FROM sk_sources WHERE id=%s", (item["id"],)).fetchone()
                if old and old["collection_id"] != item["collection_id"]:
                    raise AccessDenied("source collection cannot be reassigned")
                if old and old["config_hash"] != item["config_hash"]:
                    # Relative external IDs may collide after changing a root.
                    # Keep the old version archive, but never serve it as a
                    # fallback for a document observed in the new configuration.
                    c.execute("UPDATE sk_documents SET public=false,acl_verified_at=NULL,active_version=NULL,"
                              "source_revision='',status='pending',error_code=NULL WHERE source_id=%s",
                              (item["id"],))
                c.execute("INSERT INTO sk_sources(id,collection_id,config_hash) VALUES(%s,%s,%s) "
                          "ON CONFLICT(id) DO UPDATE SET config_hash=excluded.config_hash,enabled=true",
                          (item["id"], item["collection_id"], item["config_hash"]))
            # Removed configured sources are immediately excluded, not kept indefinitely.
            configured = [item["id"] for item in sources]
            c.execute("UPDATE sk_sources s SET enabled=false FROM sk_collections k WHERE k.id=s.collection_id "
                      "AND k.company_id=%s AND NOT(s.id=ANY(%s::text[]))", (company_id, configured))
            c.execute("UPDATE sk_documents d SET public=false,acl_verified_at=NULL "
                      "FROM sk_sources s JOIN sk_collections k ON k.id=s.collection_id "
                      "WHERE d.source_id=s.id AND k.company_id=%s AND NOT(s.id=ANY(%s::text[]))",
                      (company_id, configured))

    @contextmanager
    def source_lock(self, source_id: str, expected_config_hash: str | None = None):
        key = int.from_bytes(hashlib.sha256(source_id.encode()).digest()[:8], "big", signed=True)
        with self.connect() as c:
            c.autocommit = True
            locked = c.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()["acquired"]
            if not locked:
                raise RuntimeError("source_sync_already_running")
            try:
                source = c.execute("SELECT enabled,config_hash FROM sk_sources WHERE id=%s", (source_id,)).fetchone()
                if not source or not source["enabled"]:
                    raise AccessDenied("source is not configured")
                if expected_config_hash is not None and source["config_hash"] != expected_config_hash:
                    raise AccessDenied("source configuration has changed; restart worker")
                yield
            finally:
                c.execute("SELECT pg_advisory_unlock(%s)", (key,))

    def observe(self, source_id: str, node: SourceNode, scanned_at: datetime) -> dict:
        doc_id = document_id(source_id, node.external_id)
        public = node.public is True
        with self.connect() as c:
            source = c.execute("SELECT enabled FROM sk_sources WHERE id=%s", (source_id,)).fetchone()
            if not source or not source["enabled"]:
                raise AccessDenied("source disabled")
            c.execute("INSERT INTO sk_documents(id,source_id,external_id,title,source_url,public,acl_verified_at,last_seen,metadata) "
                      "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET "
                      "title=excluded.title,source_url=excluded.source_url,public=excluded.public,"
                      "acl_verified_at=excluded.acl_verified_at,last_seen=excluded.last_seen,metadata=excluded.metadata,"
                      "status=CASE WHEN excluded.public THEN sk_documents.status ELSE 'unavailable' END",
                      (doc_id, source_id, node.external_id, node.title, node.source_url, public,
                       scanned_at if public else None, scanned_at, Jsonb(node.metadata)))
            return c.execute("SELECT d.*,v.pipeline_id,v.content_hash,v.raw_hash FROM sk_documents d "
                             "LEFT JOIN sk_versions v ON v.id=d.active_version WHERE d.id=%s", (doc_id,)).fetchone()

    def touch_unchanged(self, doc_id: str, revision: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE sk_documents SET source_revision=%s,status='ready',error_code=NULL WHERE id=%s AND public",
                      (revision, doc_id))

    def fail_document(self, doc_id: str, error_code: str, *, revoke: bool = False) -> None:
        with self.connect() as c:
            c.execute("UPDATE sk_documents SET status=CASE WHEN active_version IS NULL THEN 'failed' ELSE 'stale' END,"
                      "error_code=%s,public=CASE WHEN %s THEN false ELSE public END,"
                      "acl_verified_at=CASE WHEN %s THEN NULL ELSE acl_verified_at END WHERE id=%s",
                      (error_code[:100], revoke, revoke, doc_id))

    def reconcile(self, source_id: str, seen: Sequence[str]) -> int:
        with self.connect() as c:
            result = c.execute("UPDATE sk_documents SET public=false,acl_verified_at=NULL,status='deleted' "
                               "WHERE source_id=%s AND NOT(external_id=ANY(%s::text[])) AND status!='deleted'",
                               (source_id, list(seen)))
            return result.rowcount

    def finish_scan(self, source_id: str, *, complete: bool, counts: dict) -> None:
        with self.connect() as c:
            c.execute("UPDATE sk_sources SET last_scan=now(),last_success=CASE WHEN %s THEN now() ELSE last_success END,"
                      "status=%s,counts=%s WHERE id=%s",
                      (complete and not counts.get("failed"), "ready" if complete and not counts.get("failed") else "partial",
                       Jsonb(counts), source_id))

    def cached_embeddings(self, model_id: str, dimensions: int, hashes: Sequence[str]) -> dict[str, list[float]]:
        with self.connect() as c:
            rows = c.execute("SELECT input_hash,embedding::text AS embedding FROM sk_embedding_cache "
                             "WHERE model_id=%s AND dimensions=%s AND input_hash=ANY(%s::text[])",
                             (model_id, dimensions, list(hashes))).fetchall()
        import json
        return {row["input_hash"]: json.loads(row["embedding"]) for row in rows}

    def publish(self, *, doc_id: str, node: SourceNode, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]],
                token_texts: Sequence[str], model_id: str, dimensions: int, pipeline_id: str,
                raw_hash: str, content_hash: str, raw_path: str, warnings: Sequence[str]) -> str:
        if not chunks or not (len(chunks) == len(embeddings) == len(token_texts)):
            raise ValueError("empty or mismatched document index")
        vectors = [vector_literal(values, dimensions) for values in embeddings]
        version_id = "ver-" + uuid.uuid4().hex
        with self.connect() as c:
            doc = c.execute("SELECT d.public,s.enabled FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id "
                            "WHERE d.id=%s FOR UPDATE OF d", (doc_id,)).fetchone()
            if not doc or not doc["public"] or not doc["enabled"]:
                raise AccessDenied("document was withdrawn before publication")
            c.execute("INSERT INTO sk_versions(id,document_id,raw_hash,content_hash,pipeline_id,model_id,dimensions,"
                      "source_revision,raw_path,warnings) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (version_id, doc_id, raw_hash, content_hash, pipeline_id, model_id, dimensions,
                       node.revision, raw_path, Jsonb(list(warnings))))
            for chunk, vector, tokens in zip(chunks, vectors, token_texts, strict=True):
                c.execute("INSERT INTO sk_chunks(version_id,chunk_id,ordinal,text,heading,embedding_text,content_hash,locator,tokens,embedding) "
                          "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s::vector)",
                          (version_id, chunk.chunk_id, chunk.ordinal, chunk.text, chunk.heading, chunk.embedding_text,
                           chunk.content_hash, Jsonb(chunk.locator), tokens, vector))
                c.execute("INSERT INTO sk_embedding_cache(model_id,dimensions,input_hash,embedding) VALUES(%s,%s,%s,%s::vector) "
                          "ON CONFLICT DO NOTHING", (model_id, dimensions, chunk.content_hash, vector))
                self._index_terms(c, version_id, chunk.chunk_id, tokens.split())
            c.execute("UPDATE sk_documents SET active_version=%s,source_revision=%s,status='ready',error_code=NULL WHERE id=%s",
                      (version_id, node.revision, doc_id))
        return version_id

    @staticmethod
    def _index_terms(c, version_id: str, chunk_id: str, tokens: Sequence[str]) -> None:
        counts = Counter(tokens)
        c.execute("DELETE FROM sk_chunk_terms WHERE version_id=%s AND chunk_id=%s", (version_id, chunk_id))
        if counts:
            with c.cursor() as cursor:
                cursor.executemany("INSERT INTO sk_chunk_terms VALUES(%s,%s,%s,%s)",
                                   [(version_id, chunk_id, term, tf) for term, tf in counts.items()])
        c.execute("UPDATE sk_chunks SET token_count=%s WHERE version_id=%s AND chunk_id=%s",
                  (len(tokens), version_id, chunk_id))

    def backfill_bm25(self, tokenizer, *, company_id: str, batch_size: int = 100) -> int:
        """Resumable active-version migration; never downloads or re-embeds documents."""
        if not 1 <= batch_size <= 1000:
            raise ValueError("invalid migration batch size")
        migrated = 0
        while True:
            with self.connect() as c:
                rows = c.execute("SELECT ch.version_id,ch.chunk_id,ch.embedding_text FROM sk_chunks ch "
                    "JOIN sk_versions v ON v.id=ch.version_id JOIN sk_documents d ON d.active_version=v.id "
                    "JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id "
                    "WHERE k.company_id=%s AND ch.token_count IS NULL "
                    "ORDER BY ch.version_id,ch.chunk_id LIMIT %s FOR UPDATE OF ch",
                    (company_id, batch_size)).fetchall()
                for row in rows:
                    self._index_terms(c, row["version_id"], row["chunk_id"], tokenizer.tokenize(row["embedding_text"]))
                migrated += len(rows)
            if not rows:
                return migrated

    def _collections(self, principal: Principal, requested: Sequence[str] | None) -> list[str]:
        if not principal.actor_id or not principal.company_id or not principal.product_id:
            raise AccessDenied("authenticated principal required")
        selected = list(principal.allowed_collections if requested is None else requested)
        if not set(selected).issubset(principal.allowed_collections):
            raise AccessDenied("collection not allowed")
        return selected

    def collections(self, principal: Principal) -> list[dict]:
        acl, args = self._collection_acl(principal, None)
        with self.connect() as c:
            return c.execute("SELECT k.id,k.title FROM sk_collections k WHERE " + acl + " ORDER BY k.id", args).fetchall()

    def _collection_acl(self, principal: Principal, requested: Sequence[str] | None) -> tuple[str, tuple]:
        selected = self._collections(principal, requested)
        return ("k.company_id=%s AND k.id=ANY(%s::text[]) "
            "AND NOT EXISTS (SELECT 1 FROM sk_employees e WHERE e.company_id=k.company_id "
            "AND e.actor_id=%s AND NOT e.active) "
            "AND (k.visibility='company' OR EXISTS (SELECT 1 FROM sk_collection_grants g "
            "WHERE g.collection_id=k.id AND g.kind=k.visibility AND "
            "((g.kind='employee' AND g.subject_id=%s) OR (g.kind='department' AND EXISTS "
            "(SELECT 1 FROM sk_employee_departments ed JOIN sk_employees e "
            "ON e.company_id=ed.company_id AND e.actor_id=ed.actor_id AND e.active "
            "WHERE ed.company_id=k.company_id AND ed.actor_id=%s AND ed.department_id=g.subject_id)))))",
            (principal.company_id, selected, principal.actor_id, principal.actor_id, principal.actor_id))

    def _acl(self, principal: Principal, requested: Sequence[str] | None) -> tuple[str, tuple]:
        acl, args = self._collection_acl(principal, requested)
        return (acl + " AND s.enabled=true AND d.public=true "
                "AND d.status IN ('ready','stale') AND d.acl_verified_at >= %s",
                (*args, datetime.now(timezone.utc) - timedelta(seconds=self.acl_ttl_seconds)))

    def retrieve(self, principal: Principal, requested: Sequence[str] | None, *, vector: Sequence[float],
                 model_id: str, dimensions: int, tokens: Sequence[str], limit: int = 40) -> tuple[list[SearchHit], list[SearchHit]]:
        if not 1 <= limit <= 100:
            raise ValueError("invalid candidate limit")
        acl, args = self._acl(principal, requested)
        base = ("SELECT d.id AS document_id,v.id AS version_id,ch.chunk_id,k.id AS collection_id,"
                "d.title,ch.text,d.source_url,ch.locator,(d.status='stale') AS stale,{score} AS score "
                "FROM sk_chunks ch JOIN sk_versions v ON v.id=ch.version_id "
                "JOIN sk_documents d ON d.id=v.document_id AND d.active_version=v.id "
                "JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id "
                "WHERE " + acl + " AND v.model_id=%s AND v.dimensions=%s {extra} ORDER BY score DESC,ch.chunk_id LIMIT %s")
        with self.connect() as c:
            # Same transaction snapshot for both channels; authorization is in each SQL query, never post-filtered.
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            if c.execute("SELECT 1 FROM sk_chunks ch JOIN sk_versions v ON v.id=ch.version_id "
                         "JOIN sk_documents d ON d.active_version=v.id JOIN sk_sources s ON s.id=d.source_id "
                         "JOIN sk_collections k ON k.id=s.collection_id WHERE " + acl +
                         " AND ch.token_count IS NULL LIMIT 1", args).fetchone():
                raise RuntimeError("bm25_index_migration_required")
            dense = c.execute(base.format(score="1-(ch.embedding <=> %s::vector)", extra=""),
                              (vector_literal(vector, dimensions), *args, model_id, dimensions, limit)).fetchall()
            lexical: list[dict] = []
            if tokens:
                # N, df and average length are computed ONLY over authorized active
                # chunks. Hidden documents cannot affect a user's BM25 scores.
                lexical = c.execute("WITH eligible AS MATERIALIZED ("
                    "SELECT ch.version_id,ch.chunk_id,ch.token_count FROM sk_chunks ch "
                    "JOIN sk_versions v ON v.id=ch.version_id "
                    "JOIN sk_documents d ON d.id=v.document_id AND d.active_version=v.id "
                    "JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id "
                    "WHERE " + acl + " AND v.model_id=%s AND v.dimensions=%s AND ch.token_count IS NOT NULL), "
                    "stats AS (SELECT count(*)::float8 AS n,avg(token_count)::float8 AS avgdl FROM eligible), "
                    "matches AS MATERIALIZED (SELECT t.*,e.token_count FROM sk_chunk_terms t "
                    "JOIN eligible e USING(version_id,chunk_id) WHERE t.term=ANY(%s::text[])), "
                    "df AS (SELECT term,count(*)::float8 AS n FROM matches GROUP BY term), "
                    "ranked AS (SELECT m.version_id,m.chunk_id,"
                    "sum(ln(1+(s.n-d.n+0.5)/(d.n+0.5))*m.tf*2.2/"
                    "(m.tf+1.2*(0.25+0.75*m.token_count/nullif(s.avgdl,0))))::float8 AS score "
                    "FROM matches m JOIN df d USING(term) CROSS JOIN stats s "
                    "GROUP BY m.version_id,m.chunk_id ORDER BY score DESC,m.chunk_id,m.version_id LIMIT %s) "
                    "SELECT d.id AS document_id,v.id AS version_id,ch.chunk_id,s.collection_id,"
                    "d.title,ch.text,d.source_url,ch.locator,(d.status='stale') AS stale,r.score "
                    "FROM ranked r JOIN sk_chunks ch USING(version_id,chunk_id) "
                    "JOIN sk_versions v ON v.id=ch.version_id JOIN sk_documents d ON d.id=v.document_id "
                    "JOIN sk_sources s ON s.id=d.source_id ORDER BY r.score DESC,ch.chunk_id,v.id",
                    (*args, model_id, dimensions, list(dict.fromkeys(tokens))[:100], limit)).fetchall()
        return [SearchHit(**row) for row in dense], [SearchHit(**row) for row in lexical]

    def revalidate_hits(self, principal: Principal, hits: Sequence[SearchHit]) -> list[SearchHit]:
        if not hits:
            return []
        acl, args = self._acl(principal, None)
        with self.connect() as c:
            rows = c.execute("SELECT d.id,d.active_version,d.status FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id "
                             "JOIN sk_collections k ON k.id=s.collection_id WHERE " + acl + " AND d.id=ANY(%s::text[])",
                             (*args, [hit.document_id for hit in hits])).fetchall()
        valid = {(row["id"], row["active_version"]): row["status"] == "stale" for row in rows}
        return [replace(hit, stale=valid[(hit.document_id, hit.version_id)])
                for hit in hits if (hit.document_id, hit.version_id) in valid]

    def read(self, principal: Principal, doc_id: str, version_id: str, offset: int, limit: int) -> dict:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid pagination")
        acl, args = self._acl(principal, None)
        with self.connect() as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            doc = c.execute("SELECT d.*,v.warnings,k.id AS collection_id FROM sk_documents d "
                            "JOIN sk_versions v ON v.id=d.active_version JOIN sk_sources s ON s.id=d.source_id "
                            "JOIN sk_collections k ON k.id=s.collection_id WHERE " + acl + " AND d.id=%s AND d.active_version=%s",
                            (*args, doc_id, version_id)).fetchone()
            if not doc:
                raise NotFound("document or active version not available")
            sections = c.execute("SELECT chunk_id,ordinal,text,heading,locator FROM sk_chunks WHERE version_id=%s "
                                 "ORDER BY ordinal OFFSET %s LIMIT %s", (version_id, offset, limit + 1)).fetchall()
        more = len(sections) > limit
        sections = sections[:limit]
        return {"document_id": doc_id, "version_id": version_id, "collection_id": doc["collection_id"],
                "title": doc["title"], "source_url": doc["source_url"], "stale": doc["status"] == "stale",
                "warnings": doc["warnings"], "sections": sections,
                "citations": [{"document_id": doc_id, "version_id": version_id, "source_url": doc["source_url"],
                               "title": doc["title"], "locator": row["locator"]} for row in sections],
                "next_offset": offset + limit if more else None}

    def status(self) -> list[dict]:
        with self.connect() as c:
            return c.execute("SELECT id,collection_id,last_scan,last_success,status,counts FROM sk_sources ORDER BY id").fetchall()
