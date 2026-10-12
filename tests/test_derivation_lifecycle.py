from dataclasses import replace
from datetime import datetime, timezone

from test_postgres_e2e import case as _case, checked_dsn as _checked_dsn, tokenizer as _tokenizer, run

from knowseam.graph.config import GraphConfig
from knowseam.graph.domain import ChunkExtraction
from knowseam.graph.worker import GraphWorker
from knowseam.indexing.dependencies import PostgresExtractionCache
from knowseam.migrations.runner import MigrationRunner

case, checked_dsn, tokenizer = _case, _checked_dsn, _tokenizer


def setup(case):
    MigrationRunner(case.store.connect).apply()
    config = GraphConfig(
        enabled=True,
        extraction_cache=True,
        password="synthetic",
        collections={case.collection: {"entity_types": ["item"], "relation_types": ["supports"]}},
        extractor={"model_id": "fixed", "model_revision": "v1", "base_url": "http://127.0.0.1:8999"},
    )
    case.store.graph_projections = {case.collection: config.projection_key(case.collection)}

    class Extractor:
        calls = []

        async def extract(self, chunk, ontology):
            self.calls.append(chunk.text)
            return ChunkExtraction(entities=(), relations=())

    class Projection:
        documents = []

        def replace_document(self, snapshot):
            self.documents.append(snapshot)

    extractor, projection = Extractor(), Projection()
    cache = PostgresExtractionCache(case.store)
    worker = GraphWorker(case.store, projection, extractor, config, case.company, extraction_cache=cache)
    return worker, extractor, projection, cache, config


def test_only_changed_text_reextracted_and_new_evidence_rebound(case):
    worker, extractor, projection, cache, config = setup(case)
    case.write("A supports B.\n\nC supports D.")
    assert case.sync()["published"] == 1
    assert run(worker.step())["status"] == "done"
    assert len(extractor.calls) == 2
    old_version = case.document()["active_version"]
    case.write("New paragraph.\n\nA supports B.\n\nC supports D.")
    assert case.sync()["published"] == 1
    assert run(worker.step())["status"] == "done"
    assert len(extractor.calls) == 3
    new = projection.documents[-1]
    assert new.version_id != old_version
    assert all(c.evidence.version_id == new.version_id for c in new.chunks)
    with case.store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM sk_extraction_cache WHERE company_id=%s", (case.company,)
            ).fetchone()["n"]
            == 3
        )
        assert (
            c.execute(
                "SELECT count(*) n FROM sk_derivation_dependencies WHERE version_id=%s", (new.version_id,)
            ).fetchone()["n"]
            == 3
        )


def test_cache_commit_rejects_revocation_and_expired_claim(case):
    worker, extractor, projection, cache, config = setup(case)
    case.write("A supports B.")
    case.sync()
    job = case.store.graph_claim(
        case.company, case.store.graph_projections, lease_seconds=600, max_attempts=3
    )
    chunk = case.store.graph_load(job).chunks[0]
    ontology = config.collections[case.collection]
    value = ChunkExtraction(entities=(), relations=())
    with case.store.connect() as c:
        c.execute("UPDATE sk_graph_jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job["id"],))
    assert cache.checkpoint(job, 0, chunk, ontology, value) is False
    replacement = case.store.graph_claim(
        case.company, case.store.graph_projections, lease_seconds=600, max_attempts=3
    )
    node = run(case.connector.scan()).nodes[0]
    case.store.observe(case.source, replace(node, public=False), datetime.now(timezone.utc))
    assert cache.checkpoint(replacement, 0, chunk, ontology, value) is False
    with case.store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM sk_extraction_cache WHERE company_id=%s", (case.company,)
            ).fetchone()["n"]
            == 0
        )


def test_corrupt_payload_recomputed_instead_of_exhausting_retries(case):
    from psycopg.types.json import Jsonb

    worker, extractor, projection, cache, config = setup(case)
    case.write("A supports B.")
    case.sync()
    assert run(worker.step())["status"] == "done"
    with case.store.connect() as c:
        c.execute(
            "UPDATE sk_extraction_cache SET extraction=%s WHERE company_id=%s",
            (
                Jsonb(
                    {
                        "entities": [{"key": "x", "type": "item", "name": "X", "quote": "invented"}],
                        "relations": [],
                    }
                ),
                case.company,
            ),
        )
    case.write("New paragraph.\n\nA supports B.")
    case.sync()
    assert run(worker.step())["status"] == "done"
    assert len(extractor.calls) == 3
