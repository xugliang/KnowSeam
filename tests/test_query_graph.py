import pytest
from knowseam.domain import Principal, SearchHit
from knowseam.graph.domain import EvidenceRef, GraphEdge


def test_query_matching_does_not_guess_single_letter_entities():
    from knowseam.graph.query import query_names
    assert query_names('what supports alpha', ['A','alpha','unknown']) == ['alpha']
    assert query_names('A supports B', ['A','B']) == ['A','B']


@pytest.mark.asyncio
async def test_different_query_selects_relations_and_final_revoke_keeps_independent_hit():
    from knowseam.graph.query import QueryAwareSearch, AuthorizedFactGraph
    from knowseam.graph.config import GraphConfig
    principal = Principal('a','co','p',('col',))
    hits = [SearchHit('d'+str(i),'v','c'+str(i),'col','title',text,'',{},1.0)
            for i,text in enumerate(('A supports B.','A owns C.'))]
    edges = [GraphEdge(EvidenceRef(company_id='co',collection_id='col',document_id=h.document_id,version_id='v',chunk_id=h.chunk_id),
                       'A','B' if i==0 else 'C','supports' if i==0 else 'owns',h.text,True,1.0) for i,h in enumerate(hits)]
    class Repository:
        revoked = False
        def authorized_fact_candidates(self, principal, collection, query, frontier, limit, excluded=()):
            assert collection=='col'
            return [e for e in edges if e.relation_type in query][:limit]
        def revalidate_fact_edges(self, principal, edges):
            return [] if self.revoked else edges
        def graph_evidence(self, principal, refs):
            return [h for h in hits if any(r.document_id==h.document_id for r in refs)
                    and not (self.revoked and h.document_id=='d0')]
    repository = Repository()
    graph = QueryAwareSearch(repository,AuthorizedFactGraph(),GraphConfig(collections={'col':{'entity_types':['p'],'relation_types':['supports','owns']}}))
    first = await graph.expand(principal,['col'],[],query='A supports',options={'strategy':'query_aware'})
    second = await graph.expand(principal,['col'],[],query='A owns',options={'strategy':'query_aware'})
    assert list(first)==[hits[0]] and list(second)==[hits[1]]
    repository.revoked = True
    assert await graph.revalidate(principal,hits,first,{('col','d1','v','c1')}) == [hits[1]]


@pytest.mark.asyncio
async def test_two_hops_uses_bridge_and_does_not_revisit_budgeted_edges():
    from knowseam.graph.query import QueryAwareSearch, AuthorizedFactGraph
    from knowseam.graph.config import GraphConfig
    principal = Principal('a','co','p',('col',))
    hits = [SearchHit('d'+str(i),'v','c'+str(i),'col','title',text,'',{},1.0)
            for i,text in enumerate(('A supports B.','B supports C.'))]
    edges = [GraphEdge(EvidenceRef(company_id='co',collection_id='col',document_id=h.document_id,
                 version_id='v',chunk_id=h.chunk_id),a,b,'supports',h.text,True,1.0)
             for h,(a,b) in zip(hits,[('A','B'),('B','C')])]
    class Repository:
        revoked = False
        closed = False
        def authorized_fact_candidates(self, principal, collection, query, frontier, limit, excluded=()):
            eligible = [e for e in edges if e not in excluded and
                        (e.subject_id in (frontier or ['A']) or e.object_id in frontier)]
            return eligible[:limit]
        def graph_evidence(self, principal, refs):
            return [h for h in hits if any(r.document_id==h.document_id for r in refs)
                    and not (self.revoked and h.document_id=='d0')]
        def revalidate_fact_edges(self, principal, candidates):
            return [e for e in candidates if not (self.closed and e==edges[0])]
    repository = Repository()
    graph = QueryAwareSearch(repository,AuthorizedFactGraph(),GraphConfig(hops=2,candidate_limit=2,
                 collections={'col':{'entity_types':['p'],'relation_types':['supports']}}))
    results = await graph.expand(principal,['col'],[],query='A supports',options={
        'strategy':'query_aware','hops':2,'candidate_limit':2,'include_paths':True})
    assert [h.document_id for h in results] == ['d0','d1']
    assert len(results.explanations[('col','d1','v','c1')]['path']) == 2
    repository.closed = True
    assert await graph.revalidate(principal,results,results) == []
    assert results.explanations == {}


@pytest.mark.asyncio
async def test_final_revocation_removes_explanation_quote():
    from knowseam.graph.query import QueryAwareSearch, AuthorizedFactGraph
    from knowseam.graph.config import GraphConfig
    principal = Principal('a','co','p',('col',))
    hit = SearchHit('d','v','c','col','title','A supports B.','',{},1.0)
    edge = GraphEdge(EvidenceRef(company_id='co',collection_id='col',document_id='d',version_id='v',chunk_id='c'),
                     'A','B','supports',hit.text,True,1.0)
    class Repository:
        reads = 0
        def authorized_fact_candidates(self, *args): return [edge]
        def revalidate_fact_edges(self, principal, edges): return edges
        def graph_evidence(self, *args):
            self.reads += 1
            return [hit] if self.reads==1 else []
    graph = QueryAwareSearch(Repository(),AuthorizedFactGraph(),GraphConfig(
        collections={'col':{'entity_types':['p'],'relation_types':['supports']}}))
    results = await graph.expand(principal,['col'],[],query='A',options={'strategy':'query_aware','include_paths':True})
    assert results == []
    assert results.explanations == {}
