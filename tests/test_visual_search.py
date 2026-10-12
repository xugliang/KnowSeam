from types import SimpleNamespace
import pytest
from knowseam.domain import Principal
from knowseam.visual.domain import MultiVector,VisualConfig,VisualSearchRequest


@pytest.mark.asyncio
async def test_visual_final_revoke_and_model_failure_never_fabricates_hits():
    from knowseam.visual.service import VisualService
    config = VisualConfig(dimensions=2)
    class Backend:
        def __init__(self):
            self.config=config
            self.store=SimpleNamespace(select_collections=lambda p,c:['allowed'],collections=lambda p:[{'id':'allowed'}])
        def candidates(self,p,c,v,limit):
            assert c==['allowed']
            return [{'asset_id':'a','document_id':'d','version_id':'v','collection_id':'allowed',
                     'title':'test','source_url':'','locator':{'page':1},'multivector':MultiVector(dimensions=2,vectors=[[1.,0.]])}]
        def revalidate(self,p,hits): return []
    class Encoder:
        fingerprint=config.fingerprint
        async def encode_queries(self,texts): return [MultiVector(dimensions=2,vectors=[[1.,0.]])]
    result = await VisualService(Backend(),Encoder()).search(Principal('a','co','p',('allowed',)),VisualSearchRequest(query='test'))
    assert result['hits']==[] and result['complete'] is False


@pytest.mark.asyncio
async def test_visual_http_routes_bind_identity_and_bytes_are_not_cached():
    from test_api import client,Service
    class Optional(Service):
        async def visual_search(self,principal,body):
            self.calls.append(('visual',principal,body))
            return {'hits':[],'visual_status':'available'}
        async def read_asset(self,principal,asset_id,version_id):
            self.calls.append(('asset',principal,asset_id,version_id))
            return b'\x89PNG\r\n\x1a\n'
    service = Optional()
    async with client(service) as http:
        response = await http.post('/v1/visual/search',json={'query':'figure','collection_ids':['public']})
        assert response.status_code==200
        image = await http.get('/v1/assets/asset-1',params={'version_id':'ver-1'})
        assert image.status_code==200 and image.headers['content-type']=='image/png'
        assert image.headers['cache-control']=='no-store'
        assert service.calls[-1][1].actor_id=='employee-1'
        narrowed = await http.get('/v1/assets/asset-1',params={'version_id':'ver-1','collection_ids':'public'})
        assert narrowed.status_code==200
        assert service.calls[-1][1].allowed_collections==('public',)
        denied = await http.get('/v1/assets/asset-1',params={'version_id':'ver-1','collection_ids':'private'})
        assert denied.status_code==404  # do not reveal protected resource existence
        for payload in ({'actor_id':'admin','query':'figure'},{'query':'figure','limit':101}):
            assert (await http.post('/v1/visual/search',json=payload)).status_code==400
