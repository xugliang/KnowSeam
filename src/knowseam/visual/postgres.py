"""Authoritative current-ACL assets and a lease-fenced visual outbox."""

from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import uuid

from psycopg.types.json import Jsonb
from ..chunking import canonical, digest
from ..domain import NotFound
from ..storage import vector_literal
from .domain import AssetRequest, MultiVector, VisualRetrievalBackend


def pack_vector(value):
    data = array("f", (n for row in value.normalized() for n in row))
    if sys.byteorder != "little":
        data.byteswap()
    return data.tobytes()


def unpack_vector(payload, count, dimensions):
    if (
        not 1 <= count <= 2048
        or not 2 <= dimensions <= 4096
        or count * dimensions > 262144
        or len(payload) != count * dimensions * 4
    ):
        raise ValueError("visual_payload_shape")
    data = array("f")
    data.frombytes(payload)
    if sys.byteorder != "little":
        data.byteswap()
    return MultiVector(
        dimensions=dimensions,
        vectors=[list(data[i : i + dimensions]) for i in range(0, len(data), dimensions)],
    )


class PostgresVisual(VisualRetrievalBackend):
    def __init__(self, store, config, file_root):
        self.store, self.config, self.file_root = store, config, Path(file_root).resolve()

    def _current(self, c, document_id):
        row = c.execute(
            "SELECT d.id,d.active_version,d.public,d.status,d.acl_verified_at,s.enabled,s.config_hash,"
            "k.company_id,v.raw_path,v.raw_hash FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id "
            "JOIN sk_collections k ON k.id=s.collection_id LEFT JOIN sk_versions v ON v.id=d.active_version "
            "WHERE d.id=%s",
            (document_id,),
        ).fetchone()
        if (
            not row
            or not row["enabled"]
            or row["status"] not in {"ready", "stale"}
            or not row["active_version"]
            or row["acl_verified_at"] is None
            or row["acl_verified_at"]
            < datetime.now(timezone.utc) - timedelta(seconds=self.store.acl_ttl_seconds)
        ):
            return None
        authority = self.store.permissions
        revision = (
            authority.admission(c, document_id)
            if authority is not None
            else ("public:" + row["acl_verified_at"].isoformat() if row["public"] else None)
        )
        if revision is None:
            return None
        return dict(row, permission_revision=revision)

    def enqueue(self, c, document_id, version_id, requests, *, retry_failed=False):
        requests = [AssetRequest.model_validate(r).model_dump(mode="json") for r in requests]
        row = self._current(c, document_id)
        if not requests or len(requests) > 500 or row is None or row["active_version"] != version_id:
            raise ValueError("visual_publication_invalid")
        if any(r["source_sha256"] != row["raw_hash"] for r in requests):
            raise ValueError("visual_source_hash_mismatch")
        c.execute(
            "INSERT INTO sk_visual_sources VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (version_id, Jsonb(requests)),
        )
        c.execute(
            "INSERT INTO sk_visual_jobs(company_id,document_id,version_id,fingerprint,config_hash,permission_revision,requests) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(version_id,fingerprint,config_hash,permission_revision) "
            "DO UPDATE SET state='pending',available_at=now(),claim_token=NULL,lease_until=NULL,error_code=NULL,"
            "attempts=CASE WHEN %s THEN 0 ELSE sk_visual_jobs.attempts END "
            "WHERE (sk_visual_jobs.state='obsolete' AND sk_visual_jobs.attempts<%s) "
            "OR (%s AND sk_visual_jobs.state IN ('failed','obsolete'))",
            (
                row["company_id"],
                document_id,
                version_id,
                self.config.fingerprint,
                row["config_hash"],
                row["permission_revision"],
                Jsonb(requests),
                retry_failed,
                self.config.max_attempts,
                retry_failed,
            ),
        )

    def ensure(self, company_id, *, retry_failed=False, limit=100, after=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("visual_reconcile_limit")
        with self.store.connect() as c:
            rows = c.execute(
                "SELECT d.id,d.active_version,x.requests FROM sk_documents d "
                "JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id "
                "JOIN sk_visual_sources x ON x.version_id=d.active_version WHERE k.company_id=%s "
                "AND (%s::text IS NULL OR d.id>%s) ORDER BY d.id LIMIT %s",
                (company_id, after, after, limit),
            ).fetchall()
        # One short transaction per document; never lock an entire company's corpus.
        for row in rows:
            with self.store.connect() as c:
                c.execute("SELECT id FROM sk_documents WHERE id=%s FOR UPDATE", (row["id"],))
                if self._current(c, row["id"]):
                    current = self._current(c, row["id"])
                    if current["active_version"] == row["active_version"]:
                        self.enqueue(
                            c, row["id"], row["active_version"], row["requests"], retry_failed=retry_failed
                        )
        return {"checked": len(rows), "next_cursor": rows[-1]["id"] if len(rows) == limit else None}

    def claim(self, company_id):
        with self.store.connect() as c:
            c.execute(
                "UPDATE sk_visual_jobs SET state='failed',error_code='attempts_exhausted' WHERE company_id=%s "
                "AND attempts>=%s AND (state IN ('pending','retry') OR (state='working' AND lease_until<=now()))",
                (company_id, self.config.max_attempts),
            )
            row = c.execute(
                "SELECT * FROM sk_visual_jobs WHERE company_id=%s AND fingerprint=%s AND attempts<%s "
                "AND ((state IN ('pending','retry') AND available_at<=now()) OR (state='working' AND lease_until<=now())) "
                "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1",
                (company_id, self.config.fingerprint, self.config.max_attempts),
            ).fetchone()
            if row is None:
                return None
            return c.execute(
                "UPDATE sk_visual_jobs SET state='working',attempts=attempts+1,claim_token=%s,"
                "lease_until=now()+%s*interval '1 second' WHERE id=%s RETURNING *",
                (uuid.uuid4().hex, self.config.lease_seconds, row["id"]),
            ).fetchone()

    def _valid(self, c, job):
        row = self._current(c, job["document_id"])
        return (
            row
            if row
            and all(row[k] == job[k] for k in ("company_id", "config_hash", "permission_revision"))
            and row["active_version"] == job["version_id"]
            and job["fingerprint"] == self.config.fingerprint
            else None
        )

    def renew(self, job):
        with self.store.connect() as c:
            if not self._valid(c, job):
                return False
            return (
                c.execute(
                    "UPDATE sk_visual_jobs SET lease_until=now()+%s*interval '1 second' "
                    "WHERE id=%s AND claim_token=%s AND state='working' AND lease_until>now() RETURNING id",
                    (self.config.lease_seconds, job["id"], job["claim_token"]),
                ).fetchone()
                is not None
            )

    def load(self, job):
        with self.store.connect() as c:
            row = self._valid(c, job)
        if row is None:
            raise NotFound("visual_job_obsolete")
        path = self.file_root / row["raw_path"]
        if path.is_symlink() or not path.resolve().is_relative_to(self.file_root):
            raise ValueError("visual_archive_path")
        with path.open("rb") as stream:
            data = stream.read(30 * 1024 * 1024 + 1)
        if len(data) > 30 * 1024 * 1024 or digest(data) != row["raw_hash"]:
            raise ValueError("visual_archive_hash")
        return data

    def finish(self, job, error=None):
        with self.store.connect() as c:
            state = (
                "obsolete"
                if error is None
                else ("failed" if job["attempts"] >= self.config.max_attempts else "retry")
            )
            c.execute(
                "UPDATE sk_visual_jobs SET state=%s,error_code=%s,available_at=now()+interval '30 seconds' "
                "WHERE id=%s AND claim_token=%s AND state='working' AND lease_until>now()",
                (state, error, job["id"], job["claim_token"]),
            )

    def commit(self, job, assets, vectors):
        if not 1 <= len(assets) <= 500 or len(assets) != len(vectors):
            raise ValueError("visual_asset_count")
        with self.store.connect() as c:
            c.execute("SELECT id FROM sk_documents WHERE id=%s FOR UPDATE", (job["document_id"],))
            if self._valid(c, job) is None:
                return False
            if not c.execute(
                "SELECT id FROM sk_visual_jobs WHERE id=%s AND claim_token=%s AND state='working' "
                "AND lease_until>now() FOR UPDATE",
                (job["id"], job["claim_token"]),
            ).fetchone():
                return False
            # Rebuild changes the permission fence; current assets are replaced in one transaction.
            ids = c.execute(
                "SELECT id FROM sk_visual_assets WHERE version_id=%s AND fingerprint=%s",
                (job["version_id"], job["fingerprint"]),
            ).fetchall()
            c.execute(
                "DELETE FROM sk_visual_vectors WHERE asset_id=ANY(%s::text[])", ([x["id"] for x in ids],)
            )
            c.execute("DELETE FROM sk_visual_assets WHERE id=ANY(%s::text[])", ([x["id"] for x in ids],))
            for ordinal, (asset, vector) in enumerate(zip(assets, vectors, strict=True)):
                vector = MultiVector.model_validate(vector)
                if vector.dimensions != self.config.dimensions:
                    raise ValueError("visual_dimensions")
                asset_id = "asset-" + digest(
                    canonical([job["version_id"], job["fingerprint"], ordinal, asset.sha256, asset.locator])
                )
                c.execute(
                    "INSERT INTO sk_visual_assets(id,version_id,fingerprint,permission_revision,config_hash,sha256,width,height,locator,data) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        asset_id,
                        job["version_id"],
                        job["fingerprint"],
                        job["permission_revision"],
                        job["config_hash"],
                        asset.sha256,
                        asset.width,
                        asset.height,
                        Jsonb(asset.locator),
                        asset.data,
                    ),
                )
                c.execute(
                    "INSERT INTO sk_visual_vectors VALUES(%s,%s,%s,%s::vector,%s)",
                    (
                        asset_id,
                        vector.dimensions,
                        len(vector.vectors),
                        vector_literal(vector.coarse(), vector.dimensions),
                        pack_vector(vector),
                    ),
                )
            c.execute("UPDATE sk_visual_jobs SET state='done',error_code=NULL WHERE id=%s", (job["id"],))
        return True

    def _scope(self, principal, collections):
        acl, args = self.store._acl(principal, collections)
        sql = (
            " FROM sk_visual_assets a JOIN sk_versions v ON v.id=a.version_id "
            "JOIN sk_documents d ON d.id=v.document_id AND d.active_version=v.id "
            "JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id "
        )
        # Current ACL is authoritative on every search/download. A private revoke/restore
        # must not resurrect output from the superseded generation.
        if self.store.permissions is not None:
            sql += "JOIN sk_document_permissions p ON p.document_id=d.id "
            acl += " AND a.permission_revision=p.revision"
        return (
            sql,
            acl + " AND a.config_hash=s.config_hash AND a.fingerprint=%s",
            (*args, self.config.fingerprint),
        )

    def candidates(self, principal, collections, vector, limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("visual_candidate_limit")
        sql, acl, args = self._scope(principal, collections)
        with self.store.connect() as c:
            rows = c.execute(
                "SELECT a.id AS asset_id,a.version_id,d.id AS document_id,k.id AS collection_id,"
                "d.title,d.source_url,a.locator,a.sha256,x.dimensions,x.token_count,x.payload "
                + sql
                + "JOIN sk_visual_vectors x ON x.asset_id=a.id WHERE "
                + acl
                + " AND x.dimensions=%s ORDER BY x.coarse <=> %s::vector,a.id LIMIT %s",
                (*args, vector.dimensions, vector_literal(vector.coarse(), vector.dimensions), limit),
            ).fetchall()
        return [
            dict(r, multivector=unpack_vector(r.pop("payload"), r.pop("token_count"), r.pop("dimensions")))
            for r in rows
        ]

    def revalidate(self, principal, hits):
        if len(hits) > 100:
            raise ValueError("visual_candidate_limit")
        sql, acl, args = self._scope(principal, None)
        with self.store.connect() as c:
            rows = c.execute(
                "SELECT a.id,a.version_id" + sql + "WHERE " + acl + " AND a.id=ANY(%s::text[])",
                (*args, [h["asset_id"] for h in hits]),
            ).fetchall()
        valid = {(r["id"], r["version_id"]) for r in rows}
        return [h for h in hits if (h["asset_id"], h["version_id"]) in valid]

    def read_asset(self, principal, asset_id, version_id):
        sql, acl, args = self._scope(principal, None)
        with self.store.connect() as c:
            row = c.execute(
                "SELECT a.id AS asset_id,a.version_id,a.data,a.sha256"
                + sql
                + "WHERE "
                + acl
                + " AND a.id=%s AND a.version_id=%s",
                (*args, asset_id, version_id),
            ).fetchone()
        if row is None:
            raise NotFound("visual_asset_not_available")
        data = bytes(row["data"])
        if len(data) > 10 * 1024 * 1024 or digest(data) != row["sha256"]:
            raise ValueError("visual_asset_integrity")
        if not self.revalidate(principal, [row]):
            raise NotFound("visual_asset_not_available")
        return data
