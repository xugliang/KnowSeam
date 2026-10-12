from dataclasses import replace

import pytest

from knowseam.graph.config import GraphConfig
from knowseam.graph.domain import ChunkExtraction, EvidenceRef, GraphChunk


def config():
    return GraphConfig(enabled=True, password='synthetic', collections={'public': {
        'entity_types': ['item'], 'relation_types': ['supports']}}, extractor={
        'model_id': 'synthetic', 'model_revision': 'r1', 'base_url': 'http://127.0.0.1:8999'})


def chunk(text='A supports B.', version='v1'):
    return GraphChunk(EvidenceRef(company_id='c', collection_id='public', document_id='d',
                                 version_id=version, chunk_id='ch'), text, {'paragraph': 1})


def test_cache_key_reuses_content_but_not_scope_or_semantics():
    from knowseam.indexing.dependencies import extraction_key
    conf, item = config(), chunk()
    fingerprint = conf.projection_key('public')
    assert extraction_key(item, fingerprint) == extraction_key(replace(item, evidence=item.evidence.model_copy(
        update={'version_id': 'v2', 'chunk_id': 'new'}), locator={'paragraph': 2}), fingerprint)
    assert extraction_key(item, fingerprint) != extraction_key(chunk('new'), fingerprint)
    assert extraction_key(item, fingerprint) != extraction_key(item, fingerprint+'changed')
    assert extraction_key(item, fingerprint) != extraction_key(replace(item, evidence=item.evidence.model_copy(
        update={'company_id': 'other'})), fingerprint)


def test_implementation_fingerprint_changes_projection(monkeypatch):
    import knowseam.graph.extraction as extraction
    conf = config()
    original = conf.projection_key('public')
    monkeypatch.setattr(extraction, 'EXTRACTION_INSTRUCTION', 'changed instruction')
    assert conf.projection_key('public') != original


def test_cached_quotes_are_revalidated_against_new_chunk():
    from knowseam.indexing.dependencies import validate_for_chunk
    conf = config()
    result = ChunkExtraction.model_validate({'entities': [
        {'key': 'a', 'name': 'A', 'type': 'item', 'quote': 'A'}], 'relations': []})
    assert validate_for_chunk(chunk(), result.model_dump(mode='json'), conf.collections['public']) == result
    with pytest.raises(ValueError):
        validate_for_chunk(chunk('No supporting text.'), result.model_dump(mode='json'), conf.collections['public'])


def test_cross_version_cache_requires_pinned_model_revision():
    from pydantic import ValidationError
    values = config().model_dump()
    values['extraction_cache'] = True
    values['extractor']['model_revision'] = ''
    with pytest.raises(ValidationError):
        GraphConfig.model_validate(values)
