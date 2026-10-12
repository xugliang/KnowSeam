"""Transactional graph outbox; Neo4j is never the document/ACL authority."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence
import uuid

from psycopg.types.json import Jsonb

from ..domain import SearchHit

GRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS sk_graph_jobs (
 id bigserial PRIMARY KEY, company_id text NOT NULL, collection_id text NOT NULL,
 document_id text NOT NULL REFERENCES sk_documents(id), version_id text,
 projection_key text NOT NULL, action text NOT NULL CHECK(action IN ('upsert','delete')),
 state text NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','working','retry','done','obsolete','failed')),
 attempts integer NOT NULL DEFAULT 0, claim_token text, lease_until timestamptz,
 available_at timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(),
 finished_at timestamptz, error_code text,
 checkpoint jsonb NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS sk_graph_jobs_claim ON sk_graph_jobs(company_id,state,available_at,id);
"""


class GraphOutboxMixin:
    """Optional PostgreSQL capability, separate from the mandatory repository ABC."""

    def _graph_admitted(self, connection, row):
        authority = getattr(self, 'permissions', None)
        permitted = bool(authority.admission(connection, row['id'])) if authority is not None else row['public']
        return bool(row['enabled'] and permitted and row.get('status', 'ready') in {'ready', 'stale'})

    def _enqueue_graph(self, connection, doc_id: str) -> None:
        if not self.graph_projections:
            return
        row = connection.execute(
            "SELECT d.id,d.active_version,d.public,d.status,s.enabled,k.company_id,k.id AS collection_id "
            "FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id "
            "JOIN sk_collections k ON k.id=s.collection_id WHERE d.id=%s", (doc_id,)).fetchone()
        if not row or row["collection_id"] not in self.graph_projections:
            return
        action = "upsert" if self._graph_admitted(connection, row) and row["active_version"] else "delete"
        connection.execute("INSERT INTO sk_graph_jobs(company_id,collection_id,document_id,version_id,"
                           "projection_key,action) VALUES(%s,%s,%s,%s,%s,%s)",
                           (row["company_id"], row["collection_id"], doc_id, row["active_version"],
                            self.graph_projections[row["collection_id"]], action))

    def graph_rebuild(self, company_id: str) -> int:
        """Queue a fresh projection from current chunks, without downloading/embedding."""
        if not self.graph_projections:
            raise ValueError("graph is disabled or has no collection ontology")
        with self.connect() as c:
            # Document locks order the new event sequence against concurrent publication.
            rows = c.execute("SELECT d.id FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id "
                             "JOIN sk_collections k ON k.id=s.collection_id WHERE k.company_id=%s "
                             "AND k.id=ANY(%s::text[]) ORDER BY d.id FOR UPDATE OF d",
                             (company_id, list(self.graph_projections))).fetchall()
            for row in rows:
                self._enqueue_graph(c, row["id"])
            return len(rows)

    def _ensure_graph(self, connection, company_id: str) -> int:
        if not self.graph_projections:
            return 0
        rows = connection.execute(
            "SELECT d.id,d.active_version,d.public,d.status,s.enabled,k.id AS collection_id,"
            "j.projection_key AS last_key,j.version_id AS last_version,j.action AS last_action "
            "FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id "
            "JOIN sk_collections k ON k.id=s.collection_id LEFT JOIN LATERAL "
            "(SELECT projection_key,version_id,action FROM sk_graph_jobs WHERE document_id=d.id "
            "ORDER BY id DESC LIMIT 1) j ON true WHERE k.company_id=%s "
            "AND k.id=ANY(%s::text[]) ORDER BY d.id FOR UPDATE OF d",
            (company_id, list(self.graph_projections))).fetchall()
        queued = 0
        for row in rows:
            # Do not publish a restored document before sync finishes its ready transition.
            if row["enabled"] and row["status"] not in {"ready", "stale"} and row['status'] not in {'unavailable', 'deleted'}:
                continue
            action = "upsert" if self._graph_admitted(connection, row) and row["active_version"] else "delete"
            if (row["last_key"] != self.graph_projections[row["collection_id"]]
                    or row["last_version"] != row["active_version"] or row["last_action"] != action):
                self._enqueue_graph(connection, row["id"])
                queued += 1
        return queued

    def graph_ensure(self, company_id: str) -> int:
        """Durably schedule missing/changed fingerprints once; failed jobs require explicit rebuild."""
        with self.connect() as c:
            # Serialize parallel startup checks so the lateral snapshot cannot double-enqueue.
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("graph-ensure:" + company_id,))
            return self._ensure_graph(c, company_id)

    def graph_claim(self, company_id: str, projections: dict[str, str], *, lease_seconds: int,
                    max_attempts: int) -> dict | None:
        if not 60 <= lease_seconds <= 3600 or not 1 <= max_attempts <= 3:
            raise ValueError("invalid graph lease or retry budget")
        token = uuid.uuid4().hex
        with self.connect() as c:
            c.execute("UPDATE sk_graph_jobs SET state='failed',error_code='LeaseExpired',finished_at=now() "
                      "WHERE company_id=%s AND (collection_id,projection_key) IN "
                      "(SELECT key,value FROM jsonb_each_text(%s::jsonb)) AND attempts >= %s "
                      "AND ((state='working' AND lease_until<=now()) OR state IN ('pending','retry'))",
                      (company_id, Jsonb(projections), max_attempts))
            row = c.execute("SELECT * FROM sk_graph_jobs WHERE company_id=%s AND (collection_id,projection_key) IN "
                            "(SELECT key,value FROM jsonb_each_text(%s::jsonb)) "
                            "AND attempts < %s AND available_at<=now() "
                            "AND (state IN ('pending','retry') OR (state='working' AND lease_until<=now())) "
                            "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
                            (company_id, Jsonb(projections), max_attempts)).fetchone()
            if row is None:
                return None
            return c.execute("UPDATE sk_graph_jobs SET state='working',attempts=attempts+1,claim_token=%s,"
                             "lease_until=now()+(%s * interval '1 second'),error_code=NULL WHERE id=%s RETURNING *",
                             (token, lease_seconds, row["id"])).fetchone()

    @staticmethod
    def _claimed(job: dict) -> tuple:
        return job["id"], job["claim_token"]

    def graph_renew(self, job: dict, lease_seconds: int) -> bool:
        if not 60 <= lease_seconds <= 3600:
            raise ValueError("invalid graph lease")
        with self.connect() as c:
            return bool(c.execute("UPDATE sk_graph_jobs SET lease_until=now()+(%s * interval '1 second') "
                                  "WHERE id=%s AND claim_token=%s AND state='working' AND lease_until>now()",
                                  (lease_seconds, *self._claimed(job))).rowcount)

    def graph_load(self, job: dict):
        from .domain import EvidenceRef, GraphChunk, GraphDocument
        with self.connect() as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            row = self._graph_current(c, job)
            if row is None:
                return None
            if job["action"] == "delete":
                chunks = ()
            else:
                chunks = tuple(GraphChunk(EvidenceRef(company_id=row["company_id"], collection_id=row["collection_id"],
                                                       document_id=row["id"], version_id=job["version_id"],
                                                       chunk_id=part["chunk_id"]), part["text"], part["locator"])
                               for part in c.execute("SELECT chunk_id,text,locator FROM sk_chunks WHERE version_id=%s "
                                                     "ORDER BY ordinal", (job["version_id"],)).fetchall())
        return GraphDocument(company_id=row["company_id"], collection_id=row["collection_id"], document_id=row["id"],
                             version_id=job["version_id"] or "", event_sequence=job["id"],
                             projection_key=job["projection_key"], extractor_version="", model_id="", model_revision="",
                             chunks=chunks)

    def _graph_current(self, connection, job: dict):
        row = connection.execute("SELECT d.*,s.enabled,k.company_id,k.id AS collection_id FROM sk_documents d "
                                 "JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id "
                                 "WHERE d.id=%s", (job["document_id"],)).fetchone()
        if (not row or row["company_id"] != job["company_id"] or row["collection_id"] != job["collection_id"]
                or self.graph_projections.get(row["collection_id"]) != job["projection_key"]):
            return None
        live = self._graph_admitted(connection, row)
        if job["action"] == "delete":
            return None if live and row["active_version"] else row
        if not live or row["active_version"] != job["version_id"]:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.acl_ttl_seconds)
        if not row["acl_verified_at"] or row["acl_verified_at"] < cutoff:
            raise RuntimeError("GraphSourceAuthorizationExpired")
        return row

    def graph_is_current(self, job: dict) -> bool:
        """Check before every inference without reloading the document's full chunk list."""
        with self.connect() as c:
            return self._graph_current(c, job) is not None

    def graph_checkpoint(self, job: dict, ordinal: int, extraction: dict) -> bool:
        if not 0 <= ordinal < 10000:
            raise ValueError("invalid graph chunk ordinal")
        with self.connect() as c:
            # Only append the next extraction, never overwrite a previous checkpoint.
            return bool(c.execute("UPDATE sk_graph_jobs SET checkpoint=checkpoint || %s::jsonb "
                                  "WHERE id=%s AND claim_token=%s AND state='working' AND lease_until>now() "
                                  "AND jsonb_array_length(checkpoint)=%s",
                                  (Jsonb([extraction]), *self._claimed(job), ordinal)).rowcount)

    def graph_complete(self, job: dict, *, obsolete: bool = False) -> bool:
        with self.connect() as c:
            return bool(c.execute("UPDATE sk_graph_jobs SET state=%s,finished_at=now(),claim_token=NULL,"
                                  "lease_until=NULL WHERE id=%s AND claim_token=%s AND state='working' "
                                  "AND lease_until>now()",
                                  ("obsolete" if obsolete else "done", *self._claimed(job))).rowcount)

    def graph_fail(self, job: dict, category: str, *, max_attempts: int, delay_seconds: int = 5) -> bool:
        if not 1 <= max_attempts <= 3 or not 0 <= delay_seconds <= 3600:
            raise ValueError("invalid graph retry budget")
        safe_category = category if category.isidentifier() and len(category) <= 100 else "GraphError"
        with self.connect() as c:
            return bool(c.execute("UPDATE sk_graph_jobs SET state=CASE WHEN attempts >= %s THEN 'failed' ELSE 'retry' END,"
                                  "error_code=%s,available_at=now()+(%s * interval '1 second'),claim_token=NULL,"
                                  "lease_until=NULL WHERE id=%s AND claim_token=%s AND state='working' "
                                  "AND lease_until>now()",
                                  (max_attempts, safe_category, delay_seconds, *self._claimed(job))).rowcount)

    def graph_status(self, company_id: str) -> list[dict]:
        with self.connect() as c:
            return c.execute("SELECT state,count(*) AS count,max(created_at) AS last_created_at "
                             "FROM sk_graph_jobs WHERE company_id=%s GROUP BY state ORDER BY state",
                             (company_id,)).fetchall()

    def graph_evidence(self, principal, references: Sequence) -> list[SearchHit]:
        """Hydrate candidates exclusively from authorized CURRENT PostgreSQL chunks."""
        if len(references) > 200:
            raise ValueError("graph evidence candidate budget exceeded")
        refs = [ref.model_dump() for ref in references if ref.company_id == principal.company_id
                and ref.collection_id in principal.allowed_collections]
        if not refs:
            return []
        acl, args = self._acl(principal, None)
        with self.connect() as c:
            rows = c.execute("WITH wanted AS (SELECT * FROM jsonb_to_recordset(%s::jsonb) "
                             "AS r(company_id text,collection_id text,document_id text,version_id text,chunk_id text)) "
                             "SELECT DISTINCT d.id AS document_id,v.id AS version_id,ch.chunk_id,k.id AS collection_id,"
                             "d.title,ch.text,d.source_url,ch.locator,(d.status='stale') AS stale,1.0::float8 AS score "
                             "FROM wanted r JOIN sk_collections k ON k.id=r.collection_id AND k.company_id=r.company_id "
                             "JOIN sk_sources s ON s.collection_id=k.id JOIN sk_documents d "
                             "ON d.source_id=s.id AND d.id=r.document_id AND d.active_version=r.version_id "
                             "JOIN sk_versions v ON v.id=d.active_version JOIN sk_chunks ch "
                             "ON ch.version_id=v.id AND ch.chunk_id=r.chunk_id WHERE " + acl,
                             (Jsonb(refs), *args)).fetchall()
        hits = {(r["document_id"], r["version_id"], r["chunk_id"]): SearchHit(**r) for r in rows}
        output = []
        seen = set()
        for ref in references:
            key = (ref.document_id, ref.version_id, ref.chunk_id)
            if key in hits and key not in seen:
                output.append(hits[key])
                seen.add(key)
        return output
