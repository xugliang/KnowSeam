"""Actual isolated Neo4j projection + PostgreSQL outbox, not LLM quality tests."""
# ruff: noqa: F811 -- imported pytest fixtures intentionally match parameter names.
from dataclasses import replace
import os
from urllib.parse import urlsplit

import pytest

from test_postgres_e2e import case, checked_dsn, tokenizer, run  # noqa: F401
from knowseam.graph.config import GraphConfig
from knowseam.graph.domain import ChunkExtraction, GraphEntity, GraphRelation
from knowseam.graph.neo4j_store import Neo4jGraphStore
from knowseam.graph.worker import GraphWorker
from knowseam.graph.search import GraphSearch
from knowseam.storage import PostgresStore


@pytest.fixture
def graph_case(case):
    uri = os.environ.get("KNOWSEAM_TEST_NEO4J_URI", "")
    if not uri:
        pytest.skip("actual Neo4j requires KNOWSEAM_TEST_NEO4J_URI at loopback test port 19847")
    parsed = urlsplit(uri)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != 19847:
        pytest.fail("refusing Neo4j except dedicated loopback test port 19847 before connection")
    config = GraphConfig(enabled=True, uri=uri, password="synthetic-test-only", collections={case.collection: {
        "entity_types": ["item"], "relation_types": ["supports"]}}, extractor={
        "model_id": "test-extraction-not-a-quality-evaluation", "base_url": "http://127.0.0.1:8999"}, hops=2)
    case.store = PostgresStore(os.environ["SUITE_KNOWLEDGE_TEST_DSN"],
                              graph_projections={case.collection: config.projection_key(case.collection)})
    case.worker.store = case.store
    case.service.store = case.service.retrieval = case.store
    graph = Neo4jGraphStore(config)
    graph.initialize()
    assert graph.ready()
    case.service.graph_search = GraphSearch(case.store, graph, config)
    yield case, graph, config
    graph.close()


class DeterministicExtractor:
    """Only substitutes external inference, keeps production worker/storage/search real."""
    async def extract(self, chunk, ontology):
        subject, _, object_ = chunk.text.rstrip(".").split()
        return ChunkExtraction(entities=(GraphEntity(key="s", name=subject, type="item", quote=subject),
                                        GraphEntity(key="o", name=object_, type="item", quote=object_)),
                               relations=(GraphRelation(subject="s", object="o", type="supports", quote=chunk.text),))


def drain(case, graph, config):
    worker = GraphWorker(case.store, graph, DeterministicExtractor(), config, case.company)
    results = []
    while True:
        result = run(worker.step())
        if result is None:
            return results
        results.append(result)
        assert result["status"] in {"done", "obsolete"}, result


def test_real_graph_two_hops_revocation_deletion_and_company_isolation(graph_case):
    case, graph, config = graph_case
    case.write("A supports B.")
    case.write("B supports C.", "second.md")
    case.write("C supports D.", "third.md")
    case.sync()
    assert len(drain(case, graph, config)) == 3
    job = case.store.graph_claim(case.company, {case.collection: config.projection_key(case.collection)},
                                lease_seconds=600, max_attempts=3)
    assert job is None
    base = case.store.read(case.principal, case.document()["id"], case.document()["active_version"], 0, 50)
    from knowseam.graph.domain import EvidenceRef
    from knowseam.domain import SearchHit
    ref = EvidenceRef(company_id=case.company, collection_id=case.collection, document_id=base["document_id"],
                      version_id=base["version_id"], chunk_id=base["sections"][0]["chunk_id"])
    seed = case.store.graph_evidence(case.principal, [ref])
    assert isinstance(seed[0], SearchHit)
    expansion = run(case.service.graph_search.expand(case.principal, [case.collection], seed))
    assert {hit.text for hit in expansion} == {"A supports B.", "B supports C.", "C supports D."}
    assert graph.seed_entities("other-company", case.collection, [], limit=10) == []

    # Revoke the bridge before any asynchronous graph cleanup. Third document must disappear.
    node = next(n for n in run(case.connector.scan()).nodes if n.external_id == "second.md")
    from datetime import datetime, timezone
    case.store.observe(case.source, replace(node, public=False), datetime.now(timezone.utc))
    expansion = run(case.service.graph_search.expand(case.principal, [case.collection], seed))
    assert {hit.text for hit in expansion} == {"A supports B."}
    drain(case, graph, config)
    case.connector.omit = {"guide.md"}
    case.sync()
    drain(case, graph, config)
    assert graph.seed_entities(case.company, case.collection, [ref], limit=10) == []
    assert case.store.graph_evidence(case.principal, [ref]) == []


def test_real_neo4j_rejects_stale_projection_and_duplicate_event(graph_case):
    case, graph, config = graph_case
    case.write("A supports B.")
    case.sync()
    job = case.store.graph_claim(case.company, {case.collection: config.projection_key(case.collection)},
                                lease_seconds=600, max_attempts=3)
    old = case.store.graph_load(job)
    old = replace(old, extractor_version="fixture-v1", model_id="fixture", model_revision="1",
                  extractions=tuple(run(DeterministicExtractor().extract(chunk, config.collections[case.collection]))
                                    for chunk in old.chunks))
    graph.replace_document(old)
    graph.replace_document(old)
    seeds = graph.seed_entities(case.company, case.collection, [old.chunks[0].evidence], limit=10)
    assert len(graph.neighbors(case.company, case.collection, seeds, limit=10)) == 1
    graph.remove_document(case.company, case.collection, old.document_id, old.event_sequence + 1)
    graph.replace_document(old)  # Expired worker cannot resurrect a newer deletion.
    assert graph.seed_entities(case.company, case.collection, [old.chunks[0].evidence], limit=10) == []


def test_real_service_graph_fusion_rerank_and_late_bridge_revocation(graph_case):
    from datetime import datetime, timezone
    from knowseam.repository import RetrievalBackend
    case, graph, config = graph_case
    case.write("A supports B.")
    case.write("B supports C.", "second.md")
    case.write("C supports D.", "third.md")
    case.sync()
    drain(case, graph, config)

    class SeedOnly(RetrievalBackend):
        def retrieve(self, *args, **kwargs):
            channels = case.store.retrieve(*args, **kwargs)
            return tuple([hit for hit in channel if hit.document_id == case.document()["id"]]
                         for channel in channels)
    case.service.retrieval = SeedOnly()
    response = case.search("A")
    assert {hit["text"] for hit in response["hits"]} == {"A supports B.", "B supports C.", "C supports D."}
    assert response["graph_status"] == "available" and case.reranker.calls
    bridge = next(node for node in run(case.connector.scan()).nodes if node.external_id == "second.md")
    case.reranker.on_score = lambda: case.store.observe(case.source, replace(bridge, public=False),
                                                       datetime.now(timezone.utc))
    response = case.search("A")
    assert {hit["text"] for hit in response["hits"]} == {"A supports B."}


def test_real_concurrent_old_and_new_versions_keep_latest_projection(graph_case):
    from concurrent.futures import ThreadPoolExecutor
    case, graph, config = graph_case
    case.write("A supports B.")
    case.sync()
    def snapshot():
        job = case.store.graph_claim(case.company, {case.collection: config.projection_key(case.collection)},
                                    lease_seconds=600, max_attempts=3)
        value = case.store.graph_load(job)
        assert case.store.graph_complete(job)
        return replace(value, extractor_version="fixture", model_id="fixture", model_revision="1",
                       extractions=tuple(run(DeterministicExtractor().extract(chunk, config.collections[case.collection]))
                                         for chunk in value.chunks))
    old = snapshot()
    case.write("A supports C.")
    case.sync()
    current = snapshot()
    assert current.event_sequence > old.event_sequence and current.version_id != old.version_id
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(graph.replace_document, [old, current, old, current]))
    assert graph.seed_entities(case.company, case.collection, [old.chunks[0].evidence], limit=10) == []
    entities = graph.seed_entities(case.company, case.collection, [current.chunks[0].evidence], limit=10)
    assert {edge.quote for edge in graph.neighbors(case.company, case.collection, entities, limit=10)} == {"A supports C."}


def test_real_query_aware_postgres_fact_slice_and_neo4j_paths(graph_case):
    from datetime import datetime, timezone
    from knowseam.graph.facts import FactRecorder
    from knowseam.graph.query import QueryAwareSearch
    from knowseam.migrations.runner import MigrationRunner

    case, graph, config = graph_case
    MigrationRunner(case.store.connect).apply()
    case.store.history_enabled = True
    case.store.fact_ontologies = config.collections
    case.write("A supports B.")
    case.write("B supports C.", "second.md")
    case.write("X supports Y.", "unrelated.md")
    assert case.sync()['published'] == 3
    worker = GraphWorker(case.store, graph, DeterministicExtractor(), config, case.company,
                         fact_recorder=FactRecorder(case.store))
    while (result := run(worker.step())) is not None:
        assert result['status'] == 'done', result

    search = QueryAwareSearch(case.store, graph, config)
    candidates = run(search.expand(case.principal, [case.collection], [], query="A",
        options={'strategy': 'query_aware', 'hops': 2, 'include_paths': True}))
    assert {hit.text for hit in candidates} == {"A supports B.", "B supports C."}
    visible = run(search.revalidate(case.principal, list(candidates), candidates))
    assert len(visible) == 2
    assert any(len(item['path']) == 2 for item in candidates.explanations.values())

    # The Neo4j projection is deliberately not updated; PostgreSQL withdrawal
    # must still remove the second edge and its explanation before return.
    bridge = next(n for n in run(case.connector.scan()).nodes if n.external_id == 'second.md')
    case.store.observe(case.source, replace(bridge, public=False), datetime.now(timezone.utc))
    visible = run(search.revalidate(case.principal, list(candidates), candidates))
    assert {hit.text for hit in visible} == {"A supports B."}
    assert all(edge['quote'] != "B supports C." for item in candidates.explanations.values()
               for edge in item['path'])
