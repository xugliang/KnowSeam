import hashlib
import json


def test_options_and_source_change_fingerprint():
    from knowseam.document_parsing.protocol import ParserIdentity, DoclingOptions
    from knowseam.document_parsing.fingerprint import parser_fingerprint
    identity = ParserIdentity(upstream_sha='a'*40, runtime_source_sha256='b'*64,
                              dependency_fingerprint='c'*64, model_fingerprint='d'*64)
    first = parser_fingerprint(identity, DoclingOptions(), normalizer_version='v1')
    for other in (identity.model_copy(update={'runtime_source_sha256': 'e'*64}),
                  identity.model_copy(update={'model_fingerprint': 'e'*64})):
        assert parser_fingerprint(other, DoclingOptions(), normalizer_version='v1') != first
    assert parser_fingerprint(identity, DoclingOptions(ocr_enabled=False), normalizer_version='v1') != first
    assert parser_fingerprint(identity, DoclingOptions(), normalizer_version='v2') != first


def test_manifest_relative_integrity_and_path_independence(tmp_path):
    import pytest
    from knowseam.document_parsing.fingerprint import model_manifest_hash
    for name in ('a', 'b'):
        root = tmp_path / name
        root.mkdir()
        (root/'weight.bin').write_bytes(b'model')
        manifest = {'files': {'weight.bin': hashlib.sha256(b'model').hexdigest()}, 'sources': [{'repo':'public', 'revision':'a'*40, 'license':'Apache-2.0'}]}
        (root/'manifest.json').write_text(json.dumps(manifest))
    assert model_manifest_hash(tmp_path/'a', verify=True) == model_manifest_hash(tmp_path/'b', verify=True)
    (tmp_path/'b/weight.bin').write_bytes(b'damaged')
    with pytest.raises(ValueError):
        model_manifest_hash(tmp_path/'b', verify=True)
