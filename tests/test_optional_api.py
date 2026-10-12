import pytest
from test_api import client, Service
from knowseam.capabilities import CapabilityUnavailable


class OptionalService(Service):
    async def get_capabilities(self, principal):
        return {'context': True}

    async def context(self, principal, request):
        self.calls.append(('context', principal, request))
        if self.failure:
            raise self.failure
        return {'document_id': request.document_id, 'version_id': request.version_id,
                'collection_id': 'public', 'ranges': [], 'citations': []}

    async def table_list(self, principal, request):
        self.calls.append(('table_list', principal, request))
        return {'tables': [], 'next_offset': None}

    async def table_read(self, principal, request):
        self.calls.append(('table_read', principal, request))
        return {'cells': [], 'next_row': None}


@pytest.mark.asyncio
async def test_table_routes_bind_path_identity_and_validate_before_call():
    service = OptionalService()
    async with client(service) as http:
        response = await http.get('/v1/documents/d/tables', params={'version_id': 'v'})
        assert response.status_code == 200
        response = await http.post('/v1/documents/d/tables/t/read', json={'version_id': 'v', 'column_ids': ['c-1']})
        assert response.status_code == 200
        request = service.calls[-1][-1]
        assert (request.document_id, request.table_id, request.row_start) == ('d', 't', 1)
        before = len(service.calls)
        for changes in ({'document_id': 'other'}, {'actor_id': 'admin'}, {'row_limit': True}, {'column_ids': []}):
            response = await http.post('/v1/documents/d/tables/t/read', json={'version_id': 'v', **changes})
            assert response.status_code == 400
        assert len(service.calls) == before


@pytest.mark.asyncio
async def test_capabilities_and_context_use_trusted_identity():
    service = OptionalService()
    async with client(service) as http:
        caps = await http.get('/v1/capabilities')
        result = await http.post('/v1/context', json={'document_id': 'd', 'version_id': 'v', 'chunk_ids': ['c']})
    assert caps.status_code == result.status_code == 200
    assert caps.json() == {'context': True}
    assert service.calls[0][1].actor_id == 'employee-1'


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'actor_id': 'other'}, {'before': True}, {'before': 6},
                                    {'chunk_ids': ['x','x']}, {'max_chars': 999}])
async def test_context_rejects_identity_and_bounds(changes):
    service = OptionalService()
    async with client(service) as http:
        response = await http.post('/v1/context', json={
            'document_id': 'd', 'version_id': 'v', 'chunk_ids': ['c'], **changes})
    assert response.status_code == 400
    assert not service.calls


@pytest.mark.asyncio
async def test_missing_capability_is_not_empty_success():
    service = OptionalService()
    service.failure = CapabilityUnavailable('private exception')
    async with client(service) as http:
        response = await http.post('/v1/context', json={'document_id': 'd', 'version_id': 'v', 'chunk_ids': ['c']})
    assert response.status_code == 503
    assert response.json()['error'] == 'capability_unavailable'
    assert response.json()['error_id'] == response.headers['x-request-id']
    assert 'private' not in response.text
