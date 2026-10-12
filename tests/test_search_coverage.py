from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from knowseam.domain import Principal
from knowseam.service import KnowledgeService
from test_graph_service import AuthorizedRepository, DeterministicEmbedder, source


def test_quota_overlap_stability_and_unknown_completeness():
    from knowseam.coverage import select_coverage
    hits = [replace(source('long', 'Long', 'text', 1), chunk_id=f'c{i}', score=1,
                    locator={'section_id': 's', 'char_start': i, 'char_end': i + 100}) for i in range(4)]
    hits += [source('short', 'Short', 'text', 1)]
    selected = select_coverage(hits, max_per_document=2, limit=3)
    assert [h.document_id for h in selected.hits] == ['long', 'short']
    assert selected.coverage['complete'] is None
    assert select_coverage(list(reversed(hits)), max_per_document=2, limit=3).hits == selected.hits


def test_options_strict_values_and_limits():
    from knowseam.coverage import SearchOptions
    for value in ({'strategy': 'anything'}, {'include_diagnostics': 1}, {'filters': {'actor_id': ['boss']}},
                  {'filters': {'kind': []}}, {'filters': {'kind': ['x'] * 33}}):
        with pytest.raises(ValidationError):
            SearchOptions.model_validate(value)
    assert SearchOptions.model_validate({'strategy':'relationship','include_paths':True}).include_paths
    with pytest.raises(ValidationError):
        SearchOptions.model_validate({'include_paths':True})


class Ranker:
    timeout = 1
    def __init__(self, repo):
        self.repo, self.revoke = repo, None
    async def score(self, query, documents):
        if self.revoke:
            self.repo.revoked.add(self.revoke)
        return [1.0 for _ in documents]


@pytest.mark.asyncio
async def test_legacy_unchanged_coverage_keeps_pool_and_checks_diagnostics_grant():
    from knowseam.coverage import CoverageConfig
    from knowseam.domain import AccessDenied
    hits = [replace(source('long', 'Long', 'text', 1), chunk_id=f'c{i}') for i in range(4)]
    hits += [source('short', 'Short', 'text', 1)]
    repo = AuthorizedRepository(hits)
    repo.retrieve = lambda *a, **k: (hits, [])
    ranker = Ranker(repo)
    service = KnowledgeService(repo, DeterministicEmbedder(), ranker, SimpleNamespace(fts_text=lambda q: q),
                               coverage_config=CoverageConfig(), coverage_enabled=True, diagnostic_actor_ids=('admin',))
    actor = Principal('alice', 'company', 'product', ('public',))
    legacy = await service.search(actor, 'x', ['public'], 3)
    assert 'coverage' not in legacy and 'diagnostics' not in legacy
    assert len({h['document_id'] for h in legacy['hits']}) == 1
    value = await service.search(actor, 'x', ['public'], 3, options={'strategy': 'coverage'})
    assert {h['document_id'] for h in value['hits']} == {'long', 'short'}
    ranker.revoke = 'short'
    value = await service.search(actor, 'x', ['public'], 3, options={'strategy': 'coverage'})
    assert value['coverage']['returned_documents'] == 1
    with pytest.raises(AccessDenied):
        await service.search(actor, 'x', ['public'], options={'include_diagnostics': True})
    admin = replace(actor, actor_id='admin')
    result = await service.search(admin, 'x', ['public'], options={'include_diagnostics': True})
    assert 'stages' in result['diagnostics']
    assert 'text' not in str(result['diagnostics'])


def test_known_catalog_complete_uses_only_returned_evidence():
    from knowseam.coverage import select_coverage
    a, b = source('a', 'A', 'a', 1), source('b', 'B', 'b', 1)
    items = [{'item_id': h.document_id, 'collection_id': h.collection_id,
              'evidence': [{'document_id': h.document_id, 'version_id': h.version_id}]} for h in (a, b)]
    value = select_coverage([a], catalog_scope={'items': items, 'complete': True}, max_per_document=2, limit=5)
    assert value.coverage['complete'] is False
    value = select_coverage([a, b], catalog_scope={'items': items, 'complete': True}, max_per_document=2, limit=5)
    assert value.coverage['complete'] is True
