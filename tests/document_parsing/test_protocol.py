import base64

import httpx
import pytest

from knowseam.domain import ParsedDocument, Section


def setup_app(tmp_path):
    from knowseam.document_parsing.executor import ExecutorSettings, create_executor_app
    from knowseam.document_parsing.protocol import ParserIdentity
    identity = ParserIdentity(upstream_sha='a' * 40, runtime_source_sha256='b' * 64,
                              dependency_fingerprint='c' * 64, model_fingerprint='d' * 64)
    class Runner:
        calls = 0
        def run(self, content, options):
            self.calls += 1
            return ParsedDocument((Section(content.filename, {'page': 1}),), parser_version='test-v1')
    runner = Runner()
    app = create_executor_app(ExecutorSettings(tmp_path / 'p.sock', tmp_path, identity, max_bytes=10), runner=runner)
    return app, runner


@pytest.mark.asyncio
async def test_unicode_and_status_without_database(tmp_path):
    from knowseam.document_parsing.protocol import ParseMetadata, encode_metadata
    app, runner = setup_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://parser') as client:
        assert (await client.get('/v1/status')).json()['protocol_version'] == 1
        metadata = encode_metadata(ParseMetadata(filename='公司资料.pdf', media_type='application/pdf'))
        result = await client.post('/v1/parse', content=b'%PDF-', headers={'X-KnowSeam-Parse-Metadata': metadata})
        assert result.status_code == 200
        assert result.json()['sections'][0]['text'] == '公司资料.pdf'
    assert runner.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('header,body', [('%%%bad', b'abc'),
    (base64.b64encode(b'{"filename":"/etc/passwd","media_type":"text/plain"}').decode(), b'a'),
    (base64.b64encode(b'{"filename":"a.pdf","media_type":"application/pdf","url":"http://bad"}').decode(), b'a'),
    (base64.b64encode(b'{"filename":"a.pdf","media_type":"application/pdf"}').decode(), b'a' * 11),
    (base64.b64encode(b'{"filename":"a.pdf","media_type":"application/pdf","options":{"max_pages":201}}').decode(), b'a')])
async def test_invalid_requests_never_reach_runner(tmp_path, header, body):
    app, runner = setup_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://parser') as client:
        response = await client.post('/v1/parse', content=body, headers={'X-KnowSeam-Parse-Metadata': header})
        assert response.status_code in (400, 413)
    assert runner.calls == 0


def test_invalid_output_rejected():
    from knowseam.document_parsing.protocol import decode_document
    from knowseam.domain import UnsupportedDocument
    for body in (b'{"sections":[{"text":"a","locator":{"x":NaN}}]}',
                 b'{"sections":[],"warnings":[],"parser_version":"x"}'):
        with pytest.raises(UnsupportedDocument):
            decode_document(body, max_sections=10, max_text_chars=100)


def test_active_or_foreign_socket_is_not_unlinked(tmp_path):
    import socket
    from knowseam.document_parsing.executor import validate_socket_target
    path = tmp_path / 'p.sock'
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen()
    try:
        with pytest.raises(ValueError):
            validate_socket_target(path)
        assert path.exists()
    finally:
        server.close()
    regular = tmp_path / 'foreign'
    regular.write_text('preserve')
    with pytest.raises(ValueError):
        validate_socket_target(regular)
    assert regular.read_text() == 'preserve'


def test_owned_stale_socket_can_restart(tmp_path):
    import socket
    from knowseam.document_parsing.executor import validate_socket_target
    path = tmp_path / 'stale.sock'
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.close()
    validate_socket_target(path)
    assert not path.exists()


@pytest.mark.asyncio
async def test_cancelled_client_does_not_release_running_parser_slot(tmp_path):
    import asyncio
    import threading
    from knowseam.document_parsing.executor import ExecutorSettings, create_executor_app
    from knowseam.document_parsing.protocol import ParserIdentity, ParseMetadata, encode_metadata
    started, release = threading.Event(), threading.Event()
    class Runner:
        def run(self, content, options):
            started.set()
            assert release.wait(5)
            return ParsedDocument((Section('ok'),), parser_version='test')
    identity = ParserIdentity(upstream_sha='a'*40, runtime_source_sha256='b'*64,
                              dependency_fingerprint='c'*64, model_fingerprint='d'*64)
    app = create_executor_app(ExecutorSettings(tmp_path/'p.sock', tmp_path, identity), runner=Runner())
    headers = {'X-KnowSeam-Parse-Metadata': encode_metadata(ParseMetadata(filename='a.pdf', media_type='application/pdf'))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://parser') as client:
        first = asyncio.create_task(client.post('/v1/parse', content=b'x', headers=headers))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            response = await client.post('/v1/parse', content=b'x', headers=headers)
            assert response.status_code == 429
        finally:
            release.set()


@pytest.mark.asyncio
async def test_controlled_child_timeout_returns_504_then_same_service_recovers(tmp_path):
    import sys
    from knowseam.document_parsing.executor import ExecutorSettings, create_executor_app
    from knowseam.document_parsing.process_runner import ProcessRunner
    from knowseam.document_parsing.protocol import DoclingOptions, ParserIdentity, ParseMetadata, encode_metadata
    runner = ProcessRunner(tmp_path, command=(sys.executable, '-c', 'import time; time.sleep(10)'))
    identity = ParserIdentity(upstream_sha='a'*40, runtime_source_sha256='b'*64,
                              dependency_fingerprint='c'*64, model_fingerprint='d'*64)
    app = create_executor_app(ExecutorSettings(tmp_path/'p.sock', tmp_path, identity), runner=runner)
    headers = {'X-KnowSeam-Parse-Metadata': encode_metadata(ParseMetadata(filename='a.pdf',
        media_type='application/pdf', options=DoclingOptions(timeout_seconds=1)))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://parser') as client:
        response = await client.post('/v1/parse', content=b'x', headers=headers)
        assert response.status_code == 504
        runner.command = (sys.executable, '-c', 'print(\'{"sections":[{"text":"ok","locator":{},"heading":""}],"warnings":[],"parser_version":"test"}\')')
        response = await client.post('/v1/parse', content=b'x', headers=headers)
        assert response.status_code == 200 and response.json()['sections'][0]['text'] == 'ok'
