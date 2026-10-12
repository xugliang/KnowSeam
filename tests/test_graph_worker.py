import pytest


@pytest.mark.asyncio
async def test_worker_reuses_durable_checkpoint_after_graph_write_failure():
    from knowseam.graph.worker import GraphWorker
    from knowseam.graph.config import GraphConfig
    from knowseam.graph.domain import EvidenceRef, GraphChunk, GraphDocument, ChunkExtraction

    config = GraphConfig(enabled=True, password="synthetic-test-only", collections={"public": {
        "entity_types": ["item"], "relation_types": ["supports"]}}, extractor={
        "model_id": "test-model", "base_url": "http://127.0.0.1:8999"})
    projection = config.projection_key("public")
    chunk = GraphChunk(EvidenceRef(company_id="c", collection_id="public", document_id="d", version_id="v", chunk_id="ch"),
                       "A supports B.", {})
    snapshot = GraphDocument("c", "public", "d", "v", 1, projection, "", "", "", (chunk,))

    class Repository:
        checkpoint = []
        completed = False
        failed = False

        def graph_claim(self, *args, **kwargs):
            return {"id": 1, "claim_token": "token", "collection_id": "public", "action": "upsert", "attempts": 1,
                    "checkpoint": list(self.checkpoint)}

        def graph_load(self, job):
            return snapshot

        def graph_is_current(self, job):
            return True

        def graph_renew(self, job, lease_seconds):
            return True

        def graph_checkpoint(self, job, ordinal, value):
            self.checkpoint.append(value)
            return True

        def graph_fail(self, *args, **kwargs):
            self.failed = True
            return True

        def graph_complete(self, *args, **kwargs):
            self.completed = True
            return True

    class Extractor:
        count = 0

        async def extract(self, chunk, ontology):
            self.count += 1
            return ChunkExtraction(entities=(), relations=())

    class Store:
        fails = True

        def replace_document(self, value):
            assert value.extractions == (ChunkExtraction(entities=(), relations=()),)
            if self.fails:
                raise TimeoutError("external write timed out")

    repository, extractor, store = Repository(), Extractor(), Store()
    worker = GraphWorker(repository, store, extractor, config, "c")
    await worker.step()
    assert repository.failed and not repository.completed
    store.fails = False
    await worker.step()
    assert repository.completed and extractor.count == 1


@pytest.mark.asyncio
async def test_worker_does_not_send_remaining_chunks_after_revocation():
    from knowseam.graph.worker import GraphWorker
    from knowseam.graph.config import GraphConfig
    from knowseam.graph.domain import EvidenceRef, GraphChunk, GraphDocument, ChunkExtraction
    config = GraphConfig(enabled=True, password="synthetic-test-only", collections={"public": {
        "entity_types": ["item"], "relation_types": ["supports"]}}, extractor={
        "model_id": "test-model", "base_url": "http://127.0.0.1:8999"})
    chunks = tuple(GraphChunk(EvidenceRef(company_id="c", collection_id="public", document_id="d", version_id="v", chunk_id=key),
                              "A supports B.", {}) for key in ("ch1", "ch2"))
    snapshot = GraphDocument("c", "public", "d", "v", 1, config.projection_key("public"), "", "", "", chunks)

    class Repository:
        revoked = False
        def graph_claim(self, *args, **kwargs):
            return {"id": 1, "claim_token": "token", "collection_id": "public", "action": "upsert", "attempts": 1,
                    "checkpoint": []}
        def graph_load(self, job):
            return None if self.revoked else snapshot
        def graph_is_current(self, job):
            return not self.revoked
        def graph_renew(self, *args):
            return True
        def graph_checkpoint(self, *args):
            return True
        def graph_complete(self, *args, **kwargs):
            return True
        def graph_fail(self, *args, **kwargs):
            return True

    repository = Repository()
    class Extractor:
        calls = 0
        async def extract(self, chunk, ontology):
            self.calls += 1
            repository.revoked = True
            return ChunkExtraction(entities=(), relations=())
    class Store:
        def replace_document(self, value):
            pytest.fail("withdrawn document was projected")
    extractor = Extractor()
    await GraphWorker(repository, Store(), extractor, config, "c").step()
    assert extractor.calls == 1
