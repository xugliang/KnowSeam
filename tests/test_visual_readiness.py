from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ready", "unready", "wrong", "broken"])
async def test_visual_capability_needs_live_matching_runtime(state):
    from knowseam.capabilities import CapabilitySet
    from knowseam.domain import Principal
    from knowseam.service import KnowledgeService
    from knowseam.visual.domain import VisualConfig
    from knowseam.visual.encoder import VisualClient
    from knowseam.visual.service import VisualService
    config = VisualConfig()
    def response(request):
        assert request.method == "GET" and request.url.path == "/v1/status"
        if state == "broken":
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json={"ready": state == "ready",
            "fingerprint": "wrong" if state == "wrong" else config.fingerprint})
    encoder = VisualClient(config, transport=httpx.MockTransport(response))
    store = SimpleNamespace(collections=lambda p: [{"id": "public"}])
    visual = VisualService(SimpleNamespace(config=config), encoder)
    service = KnowledgeService(store, None, None, None, capabilities=CapabilitySet(visual=visual))
    try:
        result = await service.get_capabilities(Principal("a", "co", "p", ("public",)))
        assert result["visual"] == (state == "ready")
    finally:
        await encoder.close()
