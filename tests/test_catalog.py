import importlib.util
import pytest


def test_catalog_domain_imports_without_initializing_config_first():
    import subprocess
    import sys
    import os
    from pathlib import Path
    result = subprocess.run([sys.executable, '-c', 'from knowseam.catalog.domain import CatalogRequest'], capture_output=True,
                            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
    assert result.returncode == 0, result.stderr.decode()


def catalog():
    assert importlib.util.find_spec('knowseam.catalog'), 'source-backed catalog is missing'
    from knowseam.catalog import service
    return service


def test_cursor_bound_to_identity_and_revision():
    m = catalog()
    codec = m.CursorCodec(b'x' * 32)
    value = codec.encode(scope='identity-a', revision='r1', offset=50, now=100)
    assert codec.decode(value, scope='identity-a', revision='r1', now=200) == 50
    for scope, revision, now in [('identity-b','r1',200), ('identity-a','r2',200), ('identity-a','r1',701)]:
        with pytest.raises(ValueError, match='cursor_stale'):
            codec.decode(value, scope=scope, revision=revision, now=now)
    with pytest.raises(ValueError):
        codec.decode(value[:-2]+'xx', scope='identity-a', revision='r1', now=200)


def test_catalog_not_inferred_from_top_k_or_term_dictionary():
    m = catalog()
    page = m.catalog_page([], declared=False, complete=False, offset=0, limit=50)
    assert page['catalog_status'] == 'unavailable'
    assert page['items'] == []
    assert page['scope']['complete'] is None


def test_partial_scan_cannot_claim_complete_and_last_page_has_no_cursor_offset():
    m = catalog()
    page = m.catalog_page([{'item_id': str(i)} for i in range(8)], declared=True, complete=False, offset=5, limit=5)
    assert len(page['items']) == 3
    assert page['next_offset'] is None
    assert page['catalog_status'] == 'partial'
    assert page['scope']['complete'] is False
