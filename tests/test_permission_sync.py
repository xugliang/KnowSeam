import argparse
from pathlib import Path

import httpx
import pytest

from test_cli import settings as _settings, runtime as _runtime
from test_connectors import FeishuFixture, success
from knowseam import cli
from knowseam.connectors import FeishuWikiConnector, FilesystemConnector

settings, runtime = _settings, _runtime


@pytest.mark.asyncio
async def test_permission_cli_never_initializes_models(settings, runtime, monkeypatch):
    import knowseam.permissions.postgres as pg
    from knowseam.permissions.domain import PermissionsConfig
    from knowseam.permissions.worker import PermissionWorker

    monkeypatch.setattr(cli, "build_model", lambda *a, **k: pytest.fail("model initialized"))
    monkeypatch.setattr(pg, "PermissionAuthority", lambda store: object())
    observed = []

    async def sync(self, source, connector, config_hash):
        observed.append(source)
        return {"permissions_applied": 0}

    monkeypatch.setattr(PermissionWorker, "sync", sync)
    # build_connector normally requires a real root; permission execution itself remains real.
    settings.sources[0].root.mkdir()
    updated = settings.model_copy(update={"permissions": PermissionsConfig(enabled=True)})
    assert await cli.execute(argparse.Namespace(command="permissions-sync"), updated) == 0
    assert observed == ["local"]


@pytest.mark.asyncio
async def test_feishu_permission_check_is_resource_read_only_and_private_is_unknown():
    def handle(request):
        assert "/permissions/" in request.url.path
        token = request.url.path.split("/")[-2]
        return success(
            {"permission_public": {"link_share_entity": "tenant_readable" if token == "public" else "closed"}}
        )

    fixture = FeishuFixture(handle)
    connector = FeishuWikiConnector(
        app_id="fixture", app_secret="fixture", root_token="root", transport=httpx.MockTransport(fixture)
    )
    try:
        values = await connector.fetch_permissions(("docx:public", "docx:private", "slides:no"))
        assert values[0].state == "verified" and values[0].public
        assert values[1].state == "unknown" and values[1].reason == "unsupported"
        assert values[2].reason == "unsupported"
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_filesystem_permissions_do_not_read_contents(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("synthetic")
    connector = FilesystemConnector(tmp_path, public_files=("a.txt",))
    monkeypatch.setattr(Path, "read_bytes", lambda *a: pytest.fail("read source bytes"))
    values = await connector.fetch_permissions(("a.txt", "missing.txt"))
    assert [v.public for v in values] == [True, False]
