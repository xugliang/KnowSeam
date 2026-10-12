"""Permission safety at graph traversal boundary; no database/driver required."""
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_inaccessible_intermediate_edge_never_enters_second_frontier():
    from knowseam.graph.search import GraphSearch
    from knowseam.graph.domain import EvidenceRef, GraphEdge
    from knowseam.domain import Principal, SearchHit

    principal = Principal("alice", "c", "p", ("public",))
    seed = SearchHit("d1", "v1", "ch1", "public", "Guide", "A knows B.", "file:///guide", {}, 1.0)
    hidden = EvidenceRef(company_id="c", collection_id="public", document_id="secret", version_id="v1", chunk_id="s1")

    class Repository:
        def revalidate_hits(self, principal, hits):
            return list(hits)

        def graph_evidence(self, principal, references):
            return []  # authoritative ACL refuses the only connecting edge

    class Store:
        def seed_entities(self, company_id, collection_id, evidence, limit):
            assert len(evidence) == 1 and evidence[0].document_id == "d1"
            return ["A"]

        def neighbors(self, company_id, collection_id, entity_ids, limit):
            assert entity_ids == ["A"], "unauthorized B leaked into the next frontier"
            return [GraphEdge(hidden, "A", "B", "knows", "secret statement", True, 1.0)]

    config = SimpleNamespace(hops=2, candidate_limit=10, include_inferred=False, min_confidence=0.7,
                             collections={"public": object()})
    hits = await GraphSearch(Repository(), Store(), config).expand(principal, ["public"], [seed])
    assert hits == []


@pytest.mark.asyncio
async def test_graph_candidates_are_rehydrated_from_source_and_inference_is_off():
    from knowseam.graph.search import GraphSearch
    from knowseam.graph.domain import EvidenceRef, GraphEdge
    from knowseam.domain import Principal, SearchHit

    seed = SearchHit("d1", "v1", "ch1", "public", "Guide", "A knows B.", "file:///guide", {}, 1.0)
    hit = SearchHit("d2", "v2", "ch2", "public", "Manual", "B supports C.", "file:///manual", {}, 1.0)
    ref = EvidenceRef(company_id="c", collection_id="public", document_id="d2", version_id="v2", chunk_id="ch2")

    class Repository:
        def revalidate_hits(self, principal, hits):
            return list(hits)

        def graph_evidence(self, principal, references):
            return [value for value in [seed, hit] if any(value.document_id == ref.document_id for ref in references)]

    class Store:
        def seed_entities(self, *args, **kwargs):
            return ["B"]

        def neighbors(self, *args, **kwargs):
            return [GraphEdge(ref, "B", "C", "supports", "B supports C.", True, 0.9),
                    GraphEdge(ref, "B", "D", "supports", "invented claim", False, 0.9)]

    config = SimpleNamespace(hops=1, candidate_limit=10, include_inferred=False, min_confidence=0.7,
                             collections={"public": object()})
    hits = await GraphSearch(Repository(), Store(), config).expand(Principal("a", "c", "p", ("public",)),
                                                                 ["public"], [seed])
    assert hits == [hit]


@pytest.mark.asyncio
async def test_late_bridge_revocation_invalidates_all_dependent_descendants():
    from knowseam.graph.search import GraphSearch
    from knowseam.graph.domain import EvidenceRef, GraphEdge
    from knowseam.domain import Principal, SearchHit

    seed = SearchHit("seed", "v", "s", "public", "Seed", "A", "file:///s", {}, 1.0)
    bridge = SearchHit("bridge", "v", "b", "public", "Bridge", "A to B", "file:///b", {}, 1.0)
    descendant = SearchHit("descendant", "v", "d", "public", "Descendant", "B to C", "file:///d", {}, 1.0)
    def ref(hit):
        return EvidenceRef(company_id="c", collection_id="public", document_id=hit.document_id,
                           version_id=hit.version_id, chunk_id=hit.chunk_id)

    class Repository:
        def revalidate_hits(self, principal, hits):
            return [hit for hit in hits if hit != bridge]  # not enough: descendant itself remains readable

        def graph_evidence(self, principal, references):
            if len(references) > 1:  # final path revalidation: bridge was just revoked
                return [seed, descendant]
            return [bridge if references[0].document_id == "bridge" else descendant]

    class Store:
        def seed_entities(self, *args, **kwargs):
            return ["A"]

        def neighbors(self, company_id, collection_id, entity_ids, limit):
            if entity_ids == ["A"]:
                return [GraphEdge(ref(bridge), "A", "B", "link", bridge.text, True, 1.0)]
            return [GraphEdge(ref(descendant), "B", "C", "link", descendant.text, True, 1.0)]

    config = SimpleNamespace(hops=2, candidate_limit=10, include_inferred=False, min_confidence=0.7,
                             collections={"public": object()})
    result = await GraphSearch(Repository(), Store(), config).expand(Principal("a", "c", "p", ("public",)),
                                                                    ["public"], [seed])
    assert result == []
