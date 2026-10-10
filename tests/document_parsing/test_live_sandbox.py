import base64
import io
import json
import os
from pathlib import Path
import subprocess

import httpx
import pytest

from knowseam.document_parsing.client import ParserClient
from knowseam.document_parsing.config import DoclingOptions
from knowseam.domain import SourceContent, UnsupportedDocument

pytestmark = pytest.mark.docling_real


def test_actual_container_runtime_modules_import():
    name = os.environ.get('KNOWSEAM_TEST_PARSER_CONTAINER')
    if not name:
        pytest.skip('requires task-owned parser image')
    info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
    assert info['Config']['Labels'].get('knowseam.docling.task') == '20261009'
    subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
        '--entrypoint', 'python', info['Image'], '-c',
        'from knowseam.document_parsing import normalizer; from knowseam.document_parsing.extensions import cleanup, tables'],
        check=True, timeout=10)


def test_actual_runtime_rejects_missing_and_corrupt_models_offline():
    name = os.environ.get('KNOWSEAM_TEST_PARSER_CONTAINER')
    if not name:
        pytest.skip('requires task-owned parser container')
    info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
    assert info['Config']['Labels'].get('knowseam.docling.task') == '20261009'
    code = '''import hashlib,json,tempfile
from pathlib import Path
from knowseam.document_parsing.executor import load_identity
with tempfile.TemporaryDirectory() as directory:
    root=Path(directory)
    (root/'manifest.json').write_text(json.dumps({'files':{'weight.bin':hashlib.sha256(b'valid').hexdigest()}}))
    for body,expected in [(None,'model file missing'),(b'corrupt','model checksum mismatch')]:
        if body is not None: (root/'weight.bin').write_bytes(body)
        try: load_identity(root)
        except ValueError as error: assert str(error)==expected
        else: raise AssertionError('bad model accepted')
'''
    subprocess.run(['docker', 'exec', name, 'python', '-c', code], check=True, timeout=10)


@pytest.fixture
def socket_path():
    path = os.environ.get('KNOWSEAM_TEST_PARSER_SOCKET')
    if not path:
        pytest.skip('requires isolated live parser')
    return Path(path)


def test_live_path_and_url_metadata_are_rejected(socket_path):
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path)), base_url='http://parser') as client:
        for metadata in ({'filename':'/etc/passwd','media_type':'text/plain'},
                         {'filename':'a.pdf','media_type':'application/pdf','url':'https://example.com'}):
            header = base64.b64encode(json.dumps(metadata).encode()).decode()
            assert client.post('/v1/parse', content=b'x', headers={'X-KnowSeam-Parse-Metadata':header}).status_code == 400


def test_live_small_budget_request_is_followed_by_success(socket_path):
    from docx import Document
    doc = Document()
    doc.add_paragraph('超时后的正常解析')
    stream = io.BytesIO()
    doc.save(stream)
    content = SourceContent(stream.getvalue(), '说明.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    client = ParserClient(socket_path)
    try:
        # A fast machine may finish in one second. The deterministic sleeping
        # child test covers forced timeout; this checks actual engine recovery.
        try:
            first = client.parse(content, DoclingOptions(timeout_seconds=1))
            assert '正常解析' in str(first.sections)
        except UnsupportedDocument as error:
            assert str(error) == 'parser_http_504'
        assert '正常解析' in str(client.parse(content, DoclingOptions()).sections)
    finally:
        client.close()


def test_actual_container_has_no_network_credentials_or_docker_socket():
    name = os.environ.get('KNOWSEAM_TEST_PARSER_CONTAINER')
    if not name:
        pytest.skip('requires task-owned parser container name')
    info = json.loads(subprocess.check_output(['docker','inspect', name]))[0]
    assert info['Config']['Labels'].get('knowseam.docling.task') == '20261009'
    assert info['HostConfig']['NetworkMode'] == 'none'
    assert info['HostConfig']['ReadonlyRootfs'] and info['Config']['User'] == '10001:10001'
    assert set(m['Destination'] for m in info['Mounts']) == {'/models','/run/knowseam-parser'}
    assert set(info['HostConfig']['Tmpfs']) == {'/tmp'}
    code = "import os,socket; from pathlib import Path; assert not Path('/private').exists(); assert not Path('/var/run/docker.sock').exists(); assert not any(k.upper().endswith(('TOKEN','SECRET','PASSWORD','DSN')) for k in os.environ); s=socket.socket(); s.settimeout(1); result=s.connect_ex(('1.1.1.1',443)); assert result!=0; s.close()"
    subprocess.run(['docker','exec',name,'python','-c',code], check=True, timeout=5)
