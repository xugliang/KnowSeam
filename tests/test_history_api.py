import pytest
import httpx
from knowseam.api import create_app
from knowseam.config import ServiceClient


@pytest.mark.asyncio
async def test_history_http_aware_time_scope_and_strict_parameters():
    calls = []
    class Service:
        async def history_search(self, principal, request):
            calls.append(request)
            return {'observations': [], 'temporal_status': 'available'}
        async def history_read(self, principal, request):
            return {'sections': [], 'known_as_of': request.known_as_of.isoformat()}
    app = create_app(Service(), [ServiceClient(token='a'*40,company_id='co',product_id='prod',allowed_collections=('public',))])
    headers = {'Authorization':'Bearer '+'a'*40,'X-Actor-Id':'alice','X-Company-Id':'co','X-Product-Id':'prod'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers=headers) as client:
        body = {'query':'A','valid_as_of':'2026-01-01T08:00:00+08:00','collection_ids':['public']}
        assert (await client.post('/v1/history/search', json=body)).status_code == 200
        assert calls[0].valid_as_of.isoformat() == '2026-01-01T00:00:00+00:00'
        for change in ({'valid_as_of':'2026-01-01'}, {'limit':True}, {'actor_id':'other'}):
            assert (await client.post('/v1/history/search',json={**body,**change})).status_code == 400
        assert (await client.post('/v1/history/search',json={**body,'collection_ids':['private']})).status_code == 404
        assert len(calls) == 1
