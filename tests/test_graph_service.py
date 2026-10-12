"""Full search flow with real GraphSearch and deterministic, ACL-aware boundaries.

These fixtures establish service contracts; PostgreSQL/Neo4j integration and real
model quality are verified separately.
"""

from types import SimpleNamespace

import pytest

from knowseam.domain import Embedder, Principal, Reranker, SearchHit
from knowseam.graph.config import ChatConfig, GraphConfig
from knowseam.graph.domain import EvidenceRef, GraphEdge, GraphOntology
from knowseam.graph.neo4j_store import GraphStoreError
from knowseam.graph.search import GraphSearch
from knowseam.repository import KnowledgeRepository, RetrievalBackend
from knowseam.service import KnowledgeService


def source(document, title, text, page):
    return SearchHit(document, "v-" + document, "ch-" + document, "public", title, text,
                     "https://docs.example/" + document, {"page": page}, 1.0)


def key(value):
    return value.collection_id, value.document_id, value.version_id, value.chunk_id


class AuthorizedRepository:
    """A mutable publication ACL shared by retrieval and every graph evidence read."""

    def __init__(self, hits):
        self.hits = {key(hit): hit for hit in hits}
        self.revoked = set()
        self.fail_evidence = False

    def select_collections(self, principal, requested):
        return KnowledgeRepository.select_collections(self, principal, requested)

    def collections(self, principal):
        if principal.company_id == "company" and "public" in principal.allowed_collections:
            return [{"id": "public", "title": "Approved documents"}]
        return []

    def revalidate_hits(self, principal, hits):
        allowed = {row["id"] for row in self.collections(principal)}
        return [hit for hit in hits if hit.collection_id in allowed
                and key(hit) in self.hits and hit.document_id not in self.revoked]

    def graph_evidence(self, principal, references):
        if self.fail_evidence:
            raise RuntimeError("fixture evidence read unavailable")
        candidates = [self.hits[key(ref)] for ref in references
                      if ref.company_id == principal.company_id and key(ref) in self.hits]
        return self.revalidate_hits(principal, candidates)


class DeterministicRetrieval(RetrievalBackend):
    def __init__(self, repository, seed, dense, lexical):
        self.repository = repository
        self.dense = (seed, dense)
        self.lexical = (lexical,)

    def retrieve(self, principal, requested, *, vector, model_id, dimensions, tokens, limit=40):
        selected = self.repository.select_collections(principal, requested)
        dense = self.repository.revalidate_hits(principal, [hit for hit in self.dense
                                                           if hit.collection_id in selected])
        lexical = self.repository.revalidate_hits(principal, [hit for hit in self.lexical
                                                             if hit.collection_id in selected])
        return dense[:limit], lexical[:limit]


class DeterministicEmbedder(Embedder):
    model_id = "fixture-embedding"
    dimensions = 2

    async def embed(self, texts, *, query=False):
        return [[1.0, 0.0] for _ in texts]


class DeterministicReranker(Reranker):
    model_id = "fixture-reranking"
    timeout = 1

    def __init__(self):
        self.documents = []
        self.on_score = None

    async def score(self, query, documents):
        self.documents = list(documents)
        if self.on_score is not None:
            self.on_score()
        scores = {"Target": 0.99, "Dense": 0.8, "Lexical": 0.7, "Bridge": 0.3, "Seed": 0.1}
        return [scores[document.split("\n", 1)[0]] for document in documents]


class EvidenceGraph:
    """Graph store boundary with two hops; documents are always rehydrated by the repository."""

    def __init__(self, seed, bridge, target, dense, lexical):
        self.seed, self.bridge, self.target, self.dense, self.lexical = seed, bridge, target, dense, lexical
        self.failure = None

    @staticmethod
    def reference(hit):
        return EvidenceRef(company_id="company", collection_id=hit.collection_id,
                           document_id=hit.document_id, version_id=hit.version_id, chunk_id=hit.chunk_id)

    def seed_entities(self, company_id, collection_id, evidence, limit):
        if self.failure == "seed":
            raise GraphStoreError("fixture graph unavailable")
        if company_id != "company" or collection_id != "public":
            return []
        return ["A"] if self.reference(self.seed) in evidence else []

    def neighbors(self, company_id, collection_id, entity_ids, limit):
        if self.failure == "neighbors":
            raise GraphStoreError("fixture graph unavailable")
        if company_id != "company" or collection_id != "public":
            return []
        if "A" in entity_ids:
            edges = [GraphEdge(self.reference(self.bridge), "A", "B", "links", "A relates to B.", True, 0.9),
                     GraphEdge(self.reference(self.dense), "A", "D", "links", "Independent dense source.", True, 0.9),
                     GraphEdge(self.reference(self.lexical), "A", "E", "links", "Independent lexical source.", True, 0.9)]
        elif "B" in entity_ids:
            edges = [GraphEdge(self.reference(self.target), "B", "C", "links", "B relates to C.", True, 0.9)]
        else:
            edges = []
        return edges[:limit]


@pytest.fixture
def search_system():
    seed = source("seed", "Seed", "A appears in the approved source.", 1)
    dense = source("dense", "Dense", "Independent dense source.", 2)
    lexical = source("lexical", "Lexical", "Independent lexical source.", 3)
    bridge = source("bridge", "Bridge", "A relates to B. This is the bridge document.", 8)
    target = source("target", "Target", "B relates to C. The original source keeps its full explanation.", 12)
    repository = AuthorizedRepository([seed, dense, lexical, bridge, target])
    graph = EvidenceGraph(seed, bridge, target, dense, lexical)
    config = GraphConfig(enabled=True, password="fixture-only-password", hops=2, candidate_limit=10,
                         collections={"public": GraphOntology(entity_types=("component",), relation_types=("links",))},
                         extractor=ChatConfig(base_url="http://127.0.0.1:9000/v1", model_id="fixture-chat"))
    reranker = DeterministicReranker()
    service = KnowledgeService(repository, DeterministicEmbedder(), reranker,
                               SimpleNamespace(fts_text=lambda query: query.lower()), candidate_limit=10,
                               retrieval=DeterministicRetrieval(repository, seed, dense, lexical),
                               graph_search=GraphSearch(repository, graph, config))
    return SimpleNamespace(service=service, repository=repository, graph=graph, reranker=reranker,
                           principal=Principal("alice", "company", "product", ("public",)),
                           seed=seed, dense=dense, lexical=lexical, bridge=bridge, target=target)


async def search(system):
    return await system.service.search(system.principal, "Where is the approved relationship?", ["public"], limit=5)


def document_ids(result):
    return {hit["document_id"] for hit in result["hits"]}


@pytest.mark.asyncio
async def test_graph_only_chunk_enters_fusion_and_reranking_with_original_citation(search_system):
    result = await search(search_system)
    assert result["graph_status"] == "available"
    assert result["evidence_status"] == "candidates"
    assert "Target\nB relates to C. The original source keeps its full explanation." in search_system.reranker.documents
    assert result["hits"][0] == {
        "document_id": "target", "version_id": "v-target", "chunk_id": "ch-target", "collection_id": "public",
        "title": "Target", "text": "B relates to C. The original source keeps its full explanation.",
        "source_url": "https://docs.example/target", "locator": {"page": 12}, "score": 0.99, "stale": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["seed", "bridge"])
async def test_revocation_during_reranking_invalidates_graph_path_but_keeps_independent_channels(search_system, revoked):
    search_system.reranker.on_score = lambda: search_system.repository.revoked.add(revoked)
    result = await search(search_system)
    # The target remains independently readable. Its withdrawn connecting path
    # must still prevent the graph from returning it after the reranker finishes.
    assert "target" not in search_system.repository.revoked
    assert "Target\n" + search_system.target.text in search_system.reranker.documents
    assert document_ids(result).isdisjoint({"bridge", "target"})
    assert {"dense", "lexical"} <= document_ids(result)
    assert result["graph_status"] == "available"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["seed", "neighbors"])
async def test_graph_store_failure_degrades_to_original_dense_and_lexical_hits(search_system, failure):
    search_system.graph.failure = failure
    result = await search(search_system)
    assert document_ids(result) == {"seed", "dense", "lexical"}
    assert result["graph_status"] == "unavailable"
    assert result["evidence_status"] == "candidates"
    assert "Dense\nIndependent dense source." in search_system.reranker.documents
    assert "Lexical\nIndependent lexical source." in search_system.reranker.documents


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 5])
async def test_final_graph_path_read_failure_cannot_release_graph_only_candidates(search_system, limit):
    search_system.reranker.on_score = lambda: setattr(search_system.repository, "fail_evidence", True)
    result = await search_system.service.search(search_system.principal, "relationship", ["public"], limit=limit)
    assert "Target\n" + search_system.target.text in search_system.reranker.documents
    assert document_ids(result) == ({"dense"} if limit == 1 else {"seed", "dense", "lexical"})
    assert result["graph_status"] == "unavailable"
    assert result["evidence_status"] == "candidates"


@pytest.mark.asyncio
async def test_revocation_during_final_catalog_scope_removes_graph_path(search_system):
    from knowseam.coverage import CoverageConfig
    service = search_system.service
    service.coverage_enabled = True
    service.coverage_config = CoverageConfig(allowed_filters={'kind':['product']})
    reads = 0
    async def catalog_scope(*args):
        nonlocal reads
        reads += 1
        if reads == 2:
            search_system.repository.revoked.add('bridge')
        return {'items':[{'collection_id':h.collection_id,'evidence':[{'document_id':h.document_id,'version_id':h.version_id}]}
                         for h in search_system.repository.hits.values()], 'complete':True}
    service._catalog_scope = catalog_scope
    result = await service.search(search_system.principal,'relationship',['public'],5,
                                  options={'strategy':'coverage','filters':{'kind':['product']}})
    assert 'target' not in document_ids(result)
    assert {'dense','lexical'} <= document_ids(result)
