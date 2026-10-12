import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from knowseam.eval import EvaluationError


def runner():
    assert importlib.util.find_spec('knowseam.evaluation.runner'), 'frozen evaluation runner missing'
    from knowseam.evaluation import runner
    return runner


def test_public_bundle_contains_ten_types_and_valid_evidence():
    r = runner()
    bundle = r.load_bundle(Path(__file__).parent / 'fixtures/evaluation/v2.json')
    assert len({case.case_type for case in bundle.cases}) == 10
    assert bundle.manifest.k == 8
    assert bundle.manifest.quality_claim == 'contract_only'


@pytest.mark.asyncio
async def test_manifest_mismatch_fails_before_model_or_query(tmp_path):
    r = runner()
    bundle = r.load_bundle(Path(__file__).parent / 'fixtures/evaluation/v2.json')
    service = SimpleNamespace(embedder=SimpleNamespace(model_id='wrong'), reranker=None, tokenizer=None)
    with pytest.raises(EvaluationError, match='manifest'):
        await r.run_bundle(service, bundle)


def test_bundle_rejects_changed_identity_ref_and_duplicate_fields(tmp_path):
    r = runner()
    path = tmp_path / 'bad.json'
    value = json.loads((Path(__file__).parent/'fixtures/evaluation/v2.json').read_text(encoding='utf-8'))
    value['cases'][0]['principal_ref'] = 'other'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(EvaluationError):
        r.load_bundle(path)
    path.write_text('{"manifest":{}, "manifest":{}, "cases":[]}', encoding='utf-8')
    with pytest.raises(EvaluationError):
        r.load_bundle(path)
