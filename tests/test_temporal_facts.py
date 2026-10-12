from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


def test_temporal_query_requires_timezone_and_unknown_opt_in():
    from knowseam.history.domain import HistorySearchRequest
    with pytest.raises(ValidationError):
        HistorySearchRequest(query='price', valid_as_of='2026-01-01')
    value = HistorySearchRequest(query='price', valid_as_of='2026-01-01T08:00:00+08:00')
    assert value.valid_as_of == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert not value.include_unknown_validity


def test_multivalued_relations_not_conflicts_and_half_open_intervals():
    from knowseam.graph.facts import conflicts
    rows = [dict(observation_id='a', subject='A', subject_id='a', object_id='b', relation='supports', object='B', qualifier='',
                 valid_from=None, valid_until=None),
            dict(observation_id='b', subject='A', subject_id='a', object_id='c', relation='supports', object='C', qualifier='',
                 valid_from=None, valid_until=None)]
    assert conflicts(rows, ()) == []
    assert conflicts(rows, ('supports',)) == [('a', 'b')]
    rows[0]['valid_until'] = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows[1]['valid_from'] = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert conflicts(rows, ('supports',)) == []


def test_fact_identity_normalizes_timezone_and_keeps_object_qualifier():
    from dataclasses import replace
    from knowseam.graph.domain import ChunkExtraction, EvidenceRef, GraphChunk, GraphDocument, GraphOntology
    from knowseam.graph.facts import fact_values
    ref = EvidenceRef(company_id='co',collection_id='col',document_id='doc',version_id='v',chunk_id='c')
    chunk = GraphChunk(ref,'A uses X since 2026-01-01T00:00:00Z.',{})
    extraction = ChunkExtraction.model_validate({'entities':[
        {'key':'a','name':'A','type':'p','quote':chunk.text},
        {'key':'x','name':'X','type':'p','qualifier':'east','quote':chunk.text}],
        'relations':[{'subject':'a','object':'x','type':'uses','quote':chunk.text,'valid_from':'2026-01-01T00:00:00Z',
                      'valid_from_quote':'2026-01-01T00:00:00Z'}]})
    document = GraphDocument('co','col','doc','v',1,'projection','extractor','model','revision',(chunk,),(extraction,))
    ontology = GraphOntology(entity_types=('p',),relation_types=('uses',))
    first = fact_values(document,ontology)[0][0]
    same = extraction.model_copy(update={'relations':(extraction.relations[0].model_copy(update={
        'valid_from':datetime.fromisoformat('2026-01-01T08:00:00+08:00')}),)})
    assert fact_values(replace(document,extractions=(same,)),ontology)[0][0] == first
    other = extraction.model_copy(update={'entities':(extraction.entities[0],extraction.entities[1].model_copy(update={'qualifier':'west'}))})
    assert fact_values(replace(document,extractions=(other,)),ontology)[0][0] != first


def test_fact_extractor_factory_works_without_graph_database():
    from knowseam.graph.config import GraphConfig
    from knowseam.graph.factory import build_graph_extractor
    config = GraphConfig(extractor={'base_url':'http://127.0.0.1:8999','model_id':'synthetic'})
    assert build_graph_extractor(config) is None
    assert build_graph_extractor(config,for_facts=True) is not None


def test_conflicts_use_stable_entity_identity_not_display_name_or_object_qualifier():
    from knowseam.graph.facts import conflicts
    base = dict(observation_id='a',subject='Acme',subject_id='company-acme',relation='price',
                object='Offer',object_id='offer-east',qualifier='["","east"]',valid_from=None,valid_until=None)
    other = dict(base,observation_id='b',subject='ACME',object='Offer',object_id='offer-west',qualifier='["","west"]')
    assert conflicts([base,other],('price',)) == [('a','b')]
    unrelated = dict(other,subject='Acme',subject_id='product-acme')
    assert conflicts([base,unrelated],('price',)) == []


def test_unsubstantiated_fact_dates_are_unknown_not_model_invented_known_dates():
    from knowseam.graph.domain import ChunkExtraction, EvidenceRef, GraphChunk, GraphOntology
    from knowseam.graph.extraction import validate_extraction
    text = 'A costs B.'
    chunk = GraphChunk(EvidenceRef(company_id='co',collection_id='col',document_id='doc',version_id='v',chunk_id='c'),text,{})
    value = ChunkExtraction.model_validate({'entities':[
        {'key':'a','name':'A','type':'p','quote':text},
        {'key':'b','name':'B','type':'p','quote':text}],
        'relations':[{'subject':'a','object':'b','type':'price','quote':text,'valid_from':'1900-01-01T00:00:00Z'}]})
    checked = validate_extraction(chunk,GraphOntology(entity_types=('p',),relation_types=('price',)),value)
    assert checked.relations[0].valid_from is None
