"""Evidence completeness is not document recall, and reports contain no private labels."""
import asyncio
import importlib.util
import json

import pytest

from knowseam.domain import Principal
from knowseam.eval import EvaluationCase, EvaluationError, evaluate, save_report


def v2():
    assert importlib.util.find_spec("knowseam.evaluation") is not None, "evidence evaluation missing"
    from knowseam.evaluation.cases import EvaluationCaseV2
    return EvaluationCaseV2


def evidence_case():
    return v2().from_mapping({
        "query": "private-question", "expected_document_ids": ["private-doc"], "collection_ids": ["public"],
        "case_type": "cross_section", "principal_ref": "private-actor", "snapshot_ref": "private-snapshot",
        "required_evidence_groups": [
            {"alternatives": [{"document_id": "private-doc", "version_id": "v1", "section_id": "s0",
                               "start_char": 10, "end_char": 20}]},
            {"alternatives": [{"document_id": "private-doc", "version_id": "v1", "section_id": "s0",
                               "start_char": 30, "end_char": 40},
                              {"document_id": "private-doc", "version_id": "v1", "section_id": "s1",
                               "start_char": 0, "end_char": 10}]},
        ]})


def run_eval(case, spans):
    async def retrieve(*args):
        return {"hits": [{"document_id": "private-doc", "version_id": "v1", "collection_id": "public",
                          "score": 1.0, "locator": span} for span in spans]}
    return asyncio.run(evaluate(None, Principal("a", "c", "p", ("public",)), [case], k=8,
                               retrievers={"hybrid": retrieve}, run_kind="contract"))


def test_wrong_span_same_document_is_not_complete():
    report = run_eval(evidence_case(), [{"section_id": "s0", "char_start": 0, "char_end": 5}])
    metrics = report["modes"]["hybrid"]["metrics"]
    assert metrics["recall_at_k"] == 1
    assert metrics["evidence_group_recall"] == 0
    assert metrics["complete_support_rate"] == 0


def test_alternative_evidence_satisfies_one_group():
    report = run_eval(evidence_case(), [{"section_id": "s0", "char_start": 10, "char_end": 20},
                                      {"section_id": "s1", "char_start": 0, "char_end": 10}])
    metrics = report["modes"]["hybrid"]["metrics"]
    assert metrics["evidence_group_recall"] == 1
    assert metrics["complete_support_rate"] == 1


def test_v1_evidence_metric_is_null():
    report = run_eval(EvaluationCase("q", ("private-doc",)), [])
    assert report["modes"]["hybrid"]["metrics"].get("evidence_group_recall", "missing") is None


def test_private_labels_not_exported(tmp_path):
    report = run_eval(evidence_case(), [])
    saved = save_report(report, tmp_path / "report.json").read_text()
    assert "private-" not in saved
    assert json.loads(saved)["modes"]["hybrid"]["metrics"]["complete_support_rate"] == 0


def test_deployed_k_rejects_excess_before_call():
    with pytest.raises(EvaluationError, match="20"):
        asyncio.run(evaluate(None, Principal("a", "c", "p", ("public",)), [], k=21))


def test_deployed_explicit_callback_still_has_public_limit():
    async def callback(*args):
        return []
    with pytest.raises(EvaluationError, match='20'):
        asyncio.run(evaluate(None, Principal('a', 'c', 'p', ('public',)), [], k=21,
                             retrievers={'deployed': callback}))


def test_incomplete_cell_cannot_override_text_anchor():
    from knowseam.evidence import EvidenceSpan
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        EvidenceSpan(document_id='d', version_id='v', section_id='s', start_char=0, end_char=5, table_id='t')
