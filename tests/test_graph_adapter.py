"""Optional graph boundaries: configuration, evidence and bounded chat extraction."""

import importlib
import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import ValidationError


def module(name):
    return importlib.import_module("knowseam.graph." + name)


def ontology():
    return module("domain").GraphOntology(
        entity_types=("person", "team"), relation_types=("member_of",), aliases={"R&D": "Research"}
    )


def chat(**extra):
    return module("config").ChatConfig(
        **{"base_url": "http://127.0.0.1:9000/v1", "model_id": "test-chat", **extra}
    )


def config(**extra):
    return module("config").GraphConfig(
        **{"enabled": True, "password": "graph-fixture-secret", "collections": {"public": ontology()},
           "extractor": chat(), **extra}
    )


def chunk(text="Alice belongs to R&D."):
    domain = module("domain")
    return domain.GraphChunk(domain.EvidenceRef(company_id="company", collection_id="public",
                                               document_id="document", version_id="version", chunk_id="chunk"),
                             text, {"page": 1})


def output():
    return {"entities": [{"key": "alice", "name": "Alice", "type": "person", "quote": "Alice"},
                         {"key": "team", "name": "R&D", "type": "team", "quote": "R&D"}],
            "relations": [{"subject": "alice", "object": "team", "type": "member_of",
                           "quote": "Alice belongs to R&D.", "explicit": True, "confidence": 0.9}]}


def response(payload):
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}]}


def test_disabled_factories_do_not_import_optional_driver(monkeypatch):
    monkeypatch.setitem(sys.modules, "neo4j", None)
    factory = module("factory")
    disabled = module("config").GraphConfig()
    assert factory.build_graph_store(disabled) is None
    assert factory.build_graph_extractor(disabled) is None


@pytest.mark.parametrize("change", [{"password": ""}, {"collections": {}}, {"extractor": None}])
def test_enabled_graph_requires_explicit_credentials_ontology_and_chat(change):
    with pytest.raises(ValidationError):
        config(**change)


@pytest.mark.parametrize("change", [
    {"hops": 3}, {"hops": True}, {"candidate_limit": 0}, {"candidate_limit": 101},
    {"min_confidence": -0.1}, {"min_confidence": float("nan")}, {"poll_seconds": 301},
    {"lease_seconds": 59}, {"max_attempts": 4}, {"unknown": 1}, {"enabled": "true"},
])
def test_graph_rejects_unbounded_or_coerced_options(change):
    with pytest.raises(ValidationError):
        config(**change)


@pytest.mark.parametrize("change", [
    {"lease_seconds": 3601}, {"lease_seconds": 269},
    {"lease_seconds": 509, "extractor": {"base_url": "http://localhost:9000/v1", "model_id": "chat", "timeout": 120}},
])
def test_graph_lease_covers_bounded_extraction_and_has_an_operational_upper_bound(change):
    with pytest.raises(ValidationError):
        config(**change)


@pytest.mark.parametrize("base_url", [
    "https://provider.example/v1", "http://192.0.2.2:9000", "https://user:private@example.com/v1",
    "http://localhost:9000/v1?secret=value", "http://localhost:9000/#fragment", "ftp://localhost",
])
def test_chat_rejects_unsafe_or_unapproved_endpoints(base_url):
    with pytest.raises(ValidationError):
        chat(base_url=base_url)


def test_chat_remote_endpoint_requires_explicit_opt_in_and_secrets_are_redacted():
    configured = chat(base_url="https://provider.example/v1", allow_online=True, api_key="api-fixture-secret")
    assert configured.allow_online is True
    assert "api-fixture-secret" not in repr(configured)
    assert "graph-fixture-secret" not in repr(config())


def test_projection_fingerprint_is_stable_without_secrets_but_changes_with_semantics():
    first = config()
    reordered = module("domain").GraphOntology(
        entity_types=("team", "person"), relation_types=("member_of",), aliases={"R&D": "Research"}
    )
    same = config(password="different-secret", collections={"public": reordered},
                  extractor=chat(api_key="other-api-secret"))
    fingerprint = first.projection_key("public")
    assert len(fingerprint) == 64
    assert fingerprint == same.projection_key("public")
    assert fingerprint != config(extractor=chat(model_revision="revision-2")).projection_key("public")
    assert fingerprint != config(extractor=chat(max_chunk_chars=4000)).projection_key("public")
    assert fingerprint != config(collections={"public": ontology().model_copy(update={"version": "2"})}).projection_key("public")


@pytest.mark.parametrize("change", [
    {"hops": 2}, {"candidate_limit": 80}, {"include_inferred": True}, {"min_confidence": 0.9},
])
def test_query_tuning_does_not_invalidate_extracted_projection(change):
    assert config().projection_key("public") == config(**change).projection_key("public")


def test_endpoint_permission_without_model_change_does_not_invalidate_projection():
    assert config().projection_key("public") == config(extractor=chat(allow_online=True)).projection_key("public")


@pytest.mark.parametrize("change", [
    {"entity_types": ("person", "person")}, {"entity_types": ()}, {"relation_types": ("",)},
    {"aliases": {"": "Research"}}, {"unknown": True},
])
def test_ontology_rejects_ambiguous_or_unknown_fields(change):
    with pytest.raises(ValidationError):
        module("domain").GraphOntology(**{"entity_types": ("person",), "relation_types": ("member_of",), **change})


def test_evidence_is_frozen_and_requires_exact_scoped_identifiers():
    evidence = chunk().evidence
    with pytest.raises(ValidationError):
        evidence.company_id = "other"
    with pytest.raises(ValidationError):
        module("domain").EvidenceRef(**{**evidence.model_dump(), "chunk_id": "../chunk"})


def test_extraction_applies_only_explicit_aliases_and_keeps_exact_quotes():
    extraction = module("domain").ChunkExtraction.model_validate(output())
    checked = module("extraction").validate_extraction(chunk(), ontology(), extraction)
    assert checked.entities[1].name == "Research"
    assert checked.entities[1].quote == "R&D"
    assert checked.relations[0].quote == "Alice belongs to R&D."


@pytest.mark.parametrize("mutation", ["quote", "type", "reference", "duplicate", "unknown", "naive-time", "interval"])
def test_extraction_rejects_unproven_or_malformed_facts(mutation):
    payload = output()
    if mutation == "quote":
        payload["relations"][0]["quote"] = "Alice leads Research."
    elif mutation == "type":
        payload["entities"][0]["type"] = "invented"
    elif mutation == "reference":
        payload["relations"][0]["subject"] = "missing"
    elif mutation == "duplicate":
        payload["entities"][1]["key"] = "alice"
    elif mutation == "unknown":
        payload["entities"][0]["extra"] = "ignored?"
    elif mutation == "naive-time":
        payload["relations"][0]["valid_from"] = "2026-10-12T10:00:00"
    else:
        payload["relations"][0].update(valid_from="2026-10-12T10:00:00Z", valid_until="2026-10-11T10:00:00Z")
    with pytest.raises((ValidationError, module("extraction").ExtractionError)):
        parsed = module("domain").ChunkExtraction.model_validate(payload)
        module("extraction").validate_extraction(chunk(), ontology(), parsed)


async def test_chat_uses_json_object_with_exact_chunk_and_configured_model():
    observed = []

    def handler(request):
        observed.append((request.url.path, json.loads(request.content), request.headers["authorization"]))
        return httpx.Response(200, json=response(output()))

    extractor = module("extraction").OpenAIChatExtractor(chat(api_key="api-fixture-secret"),
                                                        transport=httpx.MockTransport(handler))
    try:
        result = await extractor.extract(chunk(), ontology())
    finally:
        await extractor.close()
    assert result.entities[1].name == "Research"
    path, body, authorization = observed[0]
    assert path == "/v1/chat/completions"
    assert body["model"] == "test-chat"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][1]["content"].endswith(chunk().text)
    assert authorization == "Bearer api-fixture-secret"


async def test_chat_retries_only_transient_failures_at_most_three_times():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("provider-secret-detail", request=request)
        return httpx.Response(503, text="provider-secret-detail")

    extractor = module("extraction").OpenAIChatExtractor(chat(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(module("extraction").ExtractionError) as caught:
            await extractor.extract(chunk(), ontology())
    finally:
        await extractor.close()
    assert len(requests) == 3
    assert "provider-secret-detail" not in str(caught.value)


@pytest.mark.parametrize("status,payload", [
    (400, response(output())), (302, response(output())), (200, {"choices": []}),
    (200, response({"entities": [], "relations": [], "unknown": "ignored?"})),
    (200, response({"entities": [], "relations": [{"subject": "x", "object": "y", "type": "member_of",
                                                 "quote": "invented"}]})),
])
async def test_chat_does_not_retry_invalid_requests_redirects_or_output(status, payload):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=payload, headers={"Location": "https://elsewhere.example"})

    extractor = module("extraction").OpenAIChatExtractor(chat(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(module("extraction").ExtractionError):
            await extractor.extract(chunk(), ontology())
    finally:
        await extractor.close()
    assert len(requests) == 1


async def test_chat_bounds_chunk_and_response_before_parsing():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))

    extractor = module("extraction").OpenAIChatExtractor(chat(max_chunk_chars=10),
                                                        transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(module("extraction").ExtractionError):
            await extractor.extract(chunk("x" * 11), ontology())
        assert requests == []
        with pytest.raises(module("extraction").ExtractionError):
            await extractor.extract(chunk("Alice"), ontology())
    finally:
        await extractor.close()
    assert len(requests) == 1


async def test_chat_bounds_total_stream_duration_and_retries_at_most_three_attempts():
    requests = []

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.02)
            yield json.dumps(response(output())).encode()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=SlowBody())

    extractor = module("extraction").OpenAIChatExtractor(chat(timeout=0.01),
                                                        transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(module("extraction").ExtractionError):
            await extractor.extract(chunk(), ontology())
    finally:
        await extractor.close()
    assert len(requests) == 3


def test_relation_preserves_timezone_aware_validity():
    relation = module("domain").GraphRelation(subject="a", object="b", type="member_of", quote="Alice",
                                             valid_from=datetime(2026, 10, 12, tzinfo=timezone.utc))
    assert relation.valid_from.tzinfo is timezone.utc


class Result:
    def __init__(self, rows=()):
        self.rows = rows

    def single(self):
        return self.rows[0] if self.rows else None

    def consume(self):
        return None

    def __iter__(self):
        return iter(self.rows)


class Transaction:
    """Narrow driver double; main tests execute these queries on real Neo4j."""
    def __init__(self, sequence=-1, rows=()):
        self.sequence = sequence
        self.rows = rows
        self.calls = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        if "RETURN d.event_sequence AS event_sequence" in query:
            return Result([{"event_sequence": self.sequence}])
        return Result(self.rows)

    def commit(self):
        self.committed = True


class Driver:
    def __init__(self, transaction):
        self.transaction = transaction
        self.timeouts = []

    def session(self, *, database):
        self.database = database
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def begin_transaction(self, *, timeout):
        self.timeouts.append(timeout)
        return self.transaction

    def verify_connectivity(self):
        return None

    def close(self):
        pass


def document(sequence=2, company="company", collection="public"):
    domain = module("domain")
    source = chunk()
    source = domain.GraphChunk(source.evidence.model_copy(update={"company_id": company, "collection_id": collection}),
                               source.text, source.locator)
    return domain.GraphDocument(company, collection, "document", "version", sequence, "a" * 64,
                                "openai-chat-v1", "test-chat", "", (source,),
                                (domain.ChunkExtraction.model_validate(output()),))


@pytest.mark.parametrize("current,attempt", [(3, 2), (2, 2)])
def test_neo4j_locks_and_fences_stale_or_duplicate_events_before_mutation(current, attempt):
    transaction = Transaction(sequence=current)
    driver = Driver(transaction)
    store = module("neo4j_store").Neo4jGraphStore(config(), driver=driver)
    store.replace_document(document(sequence=attempt))
    assert len(transaction.calls) == 1
    assert transaction.calls[0][1]["document_id"] == "document"
    assert driver.timeouts == [30]


def test_neo4j_projection_parameterizes_entities_quotes_and_owned_facts():
    transaction = Transaction()
    store = module("neo4j_store").Neo4jGraphStore(config(), driver=Driver(transaction))
    store.replace_document(document())
    mentions = next(params["rows"] for query, params in transaction.calls if "CREATE (m:KSMention" in query)
    facts = next(params["rows"] for query, params in transaction.calls if "CREATE (f:KSFact" in query)
    assert len(mentions) == 2
    assert len(facts) == 1
    assert facts[0]["company_id"] == "company"
    assert facts[0]["chunk_id"] == "chunk"
    assert facts[0]["quote"] == "Alice belongs to R&D."
    assert all("Alice belongs to R&D." not in query for query, _ in transaction.calls)
    assert transaction.committed


def test_neo4j_identity_is_scoped_by_company_and_collection():
    ids = []
    graph_config = config(collections={"public": ontology(), "other": ontology()})
    for company, collection in [("company", "public"), ("company-2", "public"), ("company", "other")]:
        transaction = Transaction()
        store = module("neo4j_store").Neo4jGraphStore(graph_config, driver=Driver(transaction))
        store.replace_document(document(company=company, collection=collection))
        entities = next(params["rows"] for query, params in transaction.calls if "MERGE (e:KSEntity" in query)
        ids.append(entities[0]["entity_id"])
    assert len(set(ids)) == 3


def test_neo4j_deletes_only_owned_evidence_and_keeps_fence_tombstone():
    transaction = Transaction(sequence=2)
    store = module("neo4j_store").Neo4jGraphStore(config(), driver=Driver(transaction))
    store.remove_document("company", "public", "document", 3)
    deletes = [(query, params) for query, params in transaction.calls if "DELETE" in query]
    assert len(deletes) == 2
    assert all("KSEntity" not in query and params["document_id"] == "document" for query, params in deletes)
    assert transaction.calls[-1][1]["event_sequence"] == 3
    assert transaction.committed


def test_neo4j_rejects_mixed_scope_before_any_write():
    transaction = Transaction()
    store = module("neo4j_store").Neo4jGraphStore(config(), driver=Driver(transaction))
    source = document(company="company-2")
    from dataclasses import replace
    with pytest.raises(module("neo4j_store").GraphStoreError):
        store.replace_document(replace(source, company_id="company"))
    assert transaction.calls == []


def test_seed_accepts_only_exact_scoped_evidence_and_bounded_limits():
    transaction = Transaction(rows=[{"entity_id": "opaque-entity"}])
    store = module("neo4j_store").Neo4jGraphStore(config(), driver=Driver(transaction))
    assert store.seed_entities("company", "public", [chunk().evidence], 10) == ["opaque-entity"]
    assert transaction.calls[-1][1]["evidence"] == [chunk().evidence.model_dump()]
    with pytest.raises(module("neo4j_store").GraphStoreError):
        store.seed_entities("other", "public", [chunk().evidence], 10)
    with pytest.raises(module("neo4j_store").GraphStoreError):
        store.neighbors("company", "public", ["opaque-entity"], 101)


def test_neighbors_return_original_evidence_and_relationship_metadata():
    row = {**chunk().evidence.model_dump(), "subject_id": "subject", "object_id": "object",
           "relation_type": "member_of", "quote": "Alice belongs to R&D.", "explicit": False,
           "confidence": 0.8, "valid_from": datetime(2026, 10, 12, tzinfo=timezone.utc), "valid_until": None}
    transaction = Transaction(rows=[row])
    store = module("neo4j_store").Neo4jGraphStore(config(), driver=Driver(transaction))
    edges = store.neighbors("company", "public", ["subject"], 10)
    assert edges[0].evidence == chunk().evidence
    assert edges[0].subject_id == "subject"
    assert edges[0].explicit is False
    assert edges[0].valid_from.tzinfo is timezone.utc
