import argparse

import pytest

from knowseam import cli
from knowseam.document_parsing.config import ParsingConfig
from test_cli import settings as _settings, runtime as _runtime

settings, runtime = _settings, _runtime


def test_build_worker_parser_owns_client_and_disabled_does_not_connect(monkeypatch):
    from knowseam.document_parsing.factory import build_parser
    from knowseam.parsers import StructuredDocumentParser
    parser = build_parser(ParsingConfig(), max_bytes=30*1024*1024)
    assert isinstance(parser, StructuredDocumentParser)


@pytest.mark.asyncio
async def test_cli_sync_uses_parser_factory(monkeypatch, settings, runtime):
    # Reuse public test fixtures; no DB or online model access.
    config = settings
    calls = []
    from knowseam.document_parsing import factory
    original = factory.build_parser
    def build(config, *, max_bytes, client=None):
        calls.append((config, client))
        return original(config, max_bytes=max_bytes, client=client)
    monkeypatch.setattr(factory, 'build_parser', build)
    class Connector:
        async def close(self):
            pass
    monkeypatch.setattr(cli, 'build_connector', lambda source: Connector())
    import knowseam.sync as sync_module
    class Worker:
        def __init__(self, store, *args, **kwargs):
            self.store = store
        async def run(self, *args, **kwargs):
            return {'failed': 0, 'published': 1}
    monkeypatch.setattr(sync_module, 'SyncWorker', Worker)
    await cli.execute(argparse.Namespace(command='sync', force=False), config)
    assert len(calls) == 1 and calls[0][0].docling.enabled is False
