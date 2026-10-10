import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def tool():
    path = ROOT / 'scripts/vendor_identity.py'
    assert path.exists(), 'vendored build identity tool missing'
    spec = importlib.util.spec_from_file_location('vendor_identity', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_upstream_identity():
    identity = json.loads((ROOT / 'third_party/docling.upstream.json').read_text())
    assert identity['upstream_sha'] == '2b3a00c5bfcd8b218d32e7e9c33d509aeaa0cb56'
    assert identity['upstream_ref'] == 'v2.135.0'
    assert identity['distribution_name'] == 'docling-slim'
    assert identity['distribution_version'] == '2.135.0'


def test_runtime_hash_is_content_not_location(tmp_path):
    module = tool()
    for name in ('a', 'b'):
        path = tmp_path / name / 'docling'
        path.mkdir(parents=True)
        (path / '__init__.py').write_text('X=1\n')
    assert module.runtime_hash(tmp_path / 'a') == module.runtime_hash(tmp_path / 'b')
    (tmp_path / 'b/docling/__init__.py').write_text('X=2\n')
    assert module.runtime_hash(tmp_path / 'a') != module.runtime_hash(tmp_path / 'b')


def test_packaging_layout_mismatch_fails_closed(tmp_path):
    import pytest
    module = tool()
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="docling"\nversion="2.135.0"\n')
    with pytest.raises(ValueError):
        module.validate_layout(tmp_path)


def test_update_refuses_dirty_worktree(tmp_path):
    script = ROOT / 'scripts/docling-upstream-update.sh'
    assert script.exists()
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'untracked.txt').write_text('preserve me')
    result = subprocess.run(['bash', str(script), 'v2.135.0', '0' * 40], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'dirty' in result.stderr.lower()
    assert (tmp_path / 'untracked.txt').read_text() == 'preserve me'


def test_installed_module_matches_vendored_runtime_source():
    import os
    import pytest
    python = os.environ.get('KNOWSEAM_TEST_DOCLING_PYTHON')
    if not python:
        pytest.skip('requires dedicated locally installed implementation environment')
    module = tool()
    code = "import importlib.metadata as m; from knowseam.document_parsing.fingerprint import installed_runtime_hash; print(m.version('docling-slim')); print(installed_runtime_hash())"
    output = subprocess.check_output([python, '-c', code], text=True).splitlines()
    assert output == ['2.135.0', module.runtime_hash(ROOT/'third_party/docling')]
