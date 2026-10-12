"""Immutable valid extraction payloads; evidence and leases are always current."""

import logging
from psycopg.types.json import Jsonb

from ..chunking import canonical, digest
from ..graph.domain import ChunkExtraction
from ..graph.extraction import ExtractionError, validate_extraction

LOGGER = logging.getLogger(__name__)


def extraction_key(chunk, fingerprint):
    ref = chunk.evidence
    return digest(canonical([ref.company_id, ref.collection_id, digest(chunk.text), fingerprint]))


def validate_for_chunk(chunk, value, ontology):
    try:
        return validate_extraction(chunk, ontology, ChunkExtraction.model_validate(value))
    except ExtractionError:
        raise ValueError("cached_extraction_evidence_invalid") from None


class PostgresExtractionCache:
    def __init__(self, store):
        self.store = store
        with store.connect() as c:
            if c.execute("SELECT to_regclass('sk_extraction_cache') name").fetchone()["name"] is None:
                raise RuntimeError("extraction_cache_migration_required")

    def get(self, job, chunk, ontology):
        with self.store.connect() as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            if self.store._graph_current(c, job) is None:
                return None
            key = extraction_key(chunk, job["projection_key"])
            row = c.execute(
                "SELECT extraction FROM sk_extraction_cache WHERE cache_id=%s AND company_id=%s "
                "AND collection_id=%s AND input_hash=%s AND semantic_fingerprint=%s",
                (
                    key,
                    chunk.evidence.company_id,
                    chunk.evidence.collection_id,
                    digest(chunk.text),
                    job["projection_key"],
                ),
            ).fetchone()
        if row is None:
            return None
        try:
            return validate_for_chunk(chunk, row["extraction"], ontology)
        except ValueError:
            LOGGER.warning("extraction_cache_invalid action=recompute")
            return None

    def checkpoint(self, job, ordinal, chunk, ontology, extraction):
        value = validate_for_chunk(chunk, extraction.model_dump(mode="json"), ontology).model_dump(
            mode="json"
        )
        with self.store.connect() as c:
            # Same document-first order as content/permission writers. Never cache a revoked result.
            c.execute("SELECT id FROM sk_documents WHERE id=%s FOR UPDATE", (job["document_id"],))
            if self.store._graph_current(c, job) is None:
                return False
            actual = c.execute(
                "SELECT chunk_id,text,locator FROM sk_chunks WHERE version_id=%s AND ordinal=%s",
                (job["version_id"], ordinal),
            ).fetchone()
            if (
                not actual
                or actual["chunk_id"] != chunk.evidence.chunk_id
                or actual["text"] != chunk.text
                or chunk.evidence.company_id != job["company_id"]
                or chunk.evidence.collection_id != job["collection_id"]
                or chunk.evidence.document_id != job["document_id"]
                or chunk.evidence.version_id != job["version_id"]
            ):
                raise ValueError("extraction_checkpoint_binding_mismatch")
            updated = c.execute(
                "UPDATE sk_graph_jobs SET checkpoint=checkpoint || %s::jsonb "
                "WHERE id=%s AND claim_token=%s AND state='working' AND lease_until>now() "
                "AND jsonb_array_length(checkpoint)=%s",
                (Jsonb([value]), job["id"], job["claim_token"], ordinal),
            ).rowcount
            if not updated:
                return False
            key = extraction_key(chunk, job["projection_key"])
            c.execute(
                "INSERT INTO sk_extraction_cache VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    key,
                    job["company_id"],
                    job["collection_id"],
                    digest(chunk.text),
                    job["projection_key"],
                    Jsonb(value),
                ),
            )
            cached = c.execute(
                "SELECT extraction FROM sk_extraction_cache WHERE cache_id=%s FOR UPDATE", (key,)
            ).fetchone()
            if cached["extraction"] != value:
                try:
                    validate_for_chunk(chunk, cached["extraction"], ontology)
                except ValueError:
                    # Repair only an invalid entry under the same source/claim fence.
                    c.execute(
                        "UPDATE sk_extraction_cache SET extraction=%s WHERE cache_id=%s", (Jsonb(value), key)
                    )
                else:
                    raise ValueError("immutable_extraction_conflict")
            c.execute(
                "INSERT INTO sk_derivation_dependencies VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (key, job["version_id"], actual["chunk_id"]),
            )
            return True
