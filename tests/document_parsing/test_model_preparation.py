import hashlib
import json
from pathlib import Path
import subprocess
import sys


def test_offline_model_import_verifies_manifest_and_needs_no_docling(tmp_path):
    source = tmp_path/'prepared'
    source.mkdir()
    (source/'weight.bin').write_bytes(b'synthetic model')
    source.chmod(0o700)
    (source/'weight.bin').chmod(0o600)
    manifest = {'files': {'weight.bin': hashlib.sha256(b'synthetic model').hexdigest()},
                'sources': [{'name': 'synthetic', 'license': 'test-only'}]}
    (source/'manifest.json').write_text(json.dumps(manifest))
    script = Path(__file__).resolve().parents[2]/'scripts/prepare_parser_models.py'
    target = tmp_path/'imported'
    command = [sys.executable, str(script), str(target), '--import-from', str(source)]
    subprocess.run(command, check=True)
    assert (target/'manifest.json').read_text() == (source/'manifest.json').read_text()
    assert (target/'weight.bin').read_bytes() == b'synthetic model'
    assert target.stat().st_mode & 0o005 == 0o005
    assert (target/'weight.bin').stat().st_mode & 0o004
    assert subprocess.run(command, capture_output=True).returncode != 0
    (source/'weight.bin').write_bytes(b'corrupt')
    failed_target = tmp_path/'failed'
    failed = subprocess.run([sys.executable, str(script), str(failed_target), '--import-from', str(source)], capture_output=True)
    assert failed.returncode != 0 and not failed_target.exists()
