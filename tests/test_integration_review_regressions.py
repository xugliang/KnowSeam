from types import SimpleNamespace

import pytest

from test_graph_service import search_system as _search_system

search_system = _search_system


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", [False, True])
async def test_revoke_independent_hit_during_last_graph_read(search_system, outage):
    f = search_system
    original = f.repository.graph_evidence
    armed = False

    def final_read(principal, refs):
        if armed:
            f.repository.revoked.add("dense")
            if outage:
                raise RuntimeError("graph unavailable")
        return original(principal, refs)

    def on_score():
        nonlocal armed
        armed = True

    f.repository.graph_evidence = final_read
    f.reranker.on_score = on_score
    result = await f.service.search(f.principal, "link", ["public"], 10)
    assert all(hit["document_id"] != "dense" for hit in result["hits"])
    assert any(hit["document_id"] == "lexical" for hit in result["hits"])


@pytest.mark.asyncio
async def test_query_graph_independent_hit_must_be_freshly_readable():
    from knowseam.domain import Principal
    from knowseam.graph.query import QueryAwareSearch, AuthorizedFactGraph
    from knowseam.graph.search import GraphCandidates, hit_key
    from test_graph_service import source

    hit = source("dense", "Dense", "Independent", 1)
    class Repository:
        def authorized_fact_candidates(self, *args):
            return []
        def revalidate_fact_edges(self, *args):
            return []
        def graph_evidence(self, principal, refs):
            return []  # revoked after the service's earlier validation

    graph = QueryAwareSearch(Repository(), AuthorizedFactGraph(), SimpleNamespace())
    candidates = GraphCandidates()
    candidates.fact_paths, candidates.include_paths = {}, True
    assert await graph.revalidate(Principal("a", "c", "p", ("public",)),
                                  [hit], candidates, {hit_key(hit)}) == []


def test_new_adapter_accepts_negotiated_old_parser_output():
    from knowseam.document_parsing.config import DoclingOptions
    from knowseam.document_parsing.docling_adapter import DoclingAdapter
    from knowseam.document_parsing.fingerprint import parser_fingerprint
    from knowseam.document_parsing.protocol import ParserIdentity
    from knowseam.domain import ParsedDocument, Section

    identity = ParserIdentity(protocol_version=1, upstream_sha="a"*40,
        runtime_source_sha256="b"*64, dependency_fingerprint="c"*64,
        model_fingerprint="d"*64, normalizer_version="docling-structure-v1-legacy")
    options = DoclingOptions()
    version = parser_fingerprint(identity, options, normalizer_version=identity.normalizer_version)
    document = ParsedDocument((Section("old compatible document"),), parser_version=version)
    client = SimpleNamespace(status=lambda: identity, parse=lambda *args: document)
    assert DoclingAdapter(client, options).parse(None) == document
