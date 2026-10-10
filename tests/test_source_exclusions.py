import asyncio

import httpx
import pytest

from knowseam.connectors.feishu import FeishuWikiConnector
from knowseam.domain import AccessDenied, SourceNode
from knowseam.config import FeishuSourceConfig


def test_excluded_node_subtree_and_same_object_alias_are_not_fetched():
    requests = []

    def node(name, obj=None, children=False):
        return dict(node_token=name, obj_token=obj or name, obj_type="sheet" if obj else "docx",
                    has_child=children, space_id="space", title=name, obj_edit_time="123")

    def handler(request):
        path = request.url.path
        requests.append(path)
        if path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "fake", "expire": 600})
        assert request.method == "GET"
        if path.endswith("get_node"):
            data = {"node": node("root", children=True)}
        elif path.endswith("/nodes"):
            assert request.url.params["parent_node_token"] == "root"
            data = {"items": [node("exam", "questions", children=True), node("alias", "questions"), node("public")], "has_more": False}
        else:
            assert "questions" not in path
            data = {"permission_public": {"link_share_entity": "tenant_readable"}}
        return httpx.Response(200, json={"code": 0, "data": data})

    async def run():
        connector = FeishuWikiConnector(app_id="a", app_secret="b", root_token="root",
            excluded_node_tokens=("exam",), excluded_object_ids=("sheet:questions",),
            transport=httpx.MockTransport(handler))
        try:
            snapshot = await connector.scan()
            assert snapshot.complete
            assert {n.external_id for n in snapshot.nodes} == {"docx:root", "docx:public"}
            count = len(requests)
            with pytest.raises(AccessDenied):
                await connector.fetch(SourceNode("sheet:questions", "exam", "1", "x", "x", True,
                    {"node_token": "alias", "obj_token": "questions", "obj_type": "sheet"}))
            assert len(requests) == count
        finally:
            await connector.close()
    asyncio.run(run())


def test_exclusion_configuration_rejects_urls_and_builds_connector():
    base = dict(type="feishu", id="s", collection_id="c", app_id="a", app_secret="b", root_token="root")
    with pytest.raises(ValueError):
        FeishuSourceConfig(**base, excluded_node_tokens=["https://example.com"])
    config = FeishuSourceConfig(**base, excluded_node_tokens=["exam"], excluded_object_ids=["sheet:questions"])
    assert config.excluded_node_tokens == ("exam",)


def test_empty_exclusions_keep_existing_source_fingerprint():
    from knowseam.cli import source_config_hash
    from knowseam.chunking import digest, canonical
    source = FeishuSourceConfig(type="feishu", id="s", collection_id="c", app_id="a", app_secret="b", root_token="root")
    old = source.model_dump(mode="json", exclude={"app_secret", "excluded_node_tokens", "excluded_object_ids"})
    assert source_config_hash(source) == digest(canonical(old))
    excluded = source.model_copy(update={"excluded_node_tokens": ("exam",)})
    assert source_config_hash(excluded) != source_config_hash(source)
