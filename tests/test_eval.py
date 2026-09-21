"""CONTRACT-only evaluation math/privacy tests; no claimed model-quality measurement."""

import asyncio
import json
from dataclasses import asdict

import pytest

from knowseam import eval as evaluation
from knowseam.domain import AccessDenied, Principal, SearchHit
from knowseam.eval import EvaluationCase, EvaluationError, evaluate, save_report
from knowseam.models import ModelError


PRINCIPAL = Principal("private-employee", "private-company", "private-product", ("public",))


def hit(document_id, collection_id="public"):
    return SearchHit(document_id, "private-version", "private-chunk", collection_id,
                     "private-title", "private-document-text", "https://private.example/wiki",
                     {"private-locator": "private-location"}, .7)


def test_recall_mrr_and_noanswer_metrics_with_duplicate_chunks():
    cases = [
        EvaluationCase("q1", ("doc-1", "doc-2")),
        EvaluationCase("q2", ("doc-3",)),
        EvaluationCase("q3", ()),
        EvaluationCase("q4", ()),
    ]
    outputs = {
        "q1": [hit("unrelated"), hit("doc-2"), hit("doc-2")],
        "q2": [hit("doc-3")], "q3": [], "q4": [hit("unrelated")],
    }

    async def contract_retriever(service, principal, case, k):
        return outputs[case.query]

    report = asyncio.run(evaluate(None, PRINCIPAL, cases, k=3,
                                  retrievers={"hybrid": contract_retriever}, run_kind="contract"))
    metrics = report["modes"]["hybrid"]["metrics"]
    assert metrics["recall_at_k"] == .75
    assert metrics["mrr_at_k"] == .75
    assert metrics["noanswer_falsepositive"] == .5
    assert metrics["noanswer_evaluated"] == 2
    assert metrics["errors"] == 0
    assert report["run_kind"] == "contract"
    assert report["quality_verified"] is False
    assert report["modes"]["hybrid"]["cases"][0]["matched_document_count"] == 1


def test_default_service_api_dict_shape_and_no_sensitive_report_fields():
    class ContractService:
        calls = []

        async def search(self, principal, query, *, collection_ids, limit):
            self.calls.append((principal, query, collection_ids, limit))
            return {"hits": [asdict(hit("private-doc"))], "debug": "private-service-debug"}

    service = ContractService()
    report = asyncio.run(evaluate(service, PRINCIPAL, [{
        "query": "private-question", "expected_document_ids": ["private-doc"], "collectionIDs": ["public"],
    }], run_kind="contract"))
    assert service.calls[0] == (PRINCIPAL, "private-question", ("public",), 10)
    serialized = json.dumps(report)
    assert "private-" not in serialized
    assert "https://" not in serialized
    assert report["modes"]["deployed"]["metrics"]["recall_at_k"] == 1


def test_modes_are_independent_and_evaluate_same_scope():
    called = []

    async def dense(service, principal, case, k):
        called.append(("dense", principal, case.collection_ids, k))
        return [hit("miss")]

    async def hybrid(service, principal, case, k):
        called.append(("hybrid", principal, case.collection_ids, k))
        return {"hits": [asdict(hit("match"))]}

    report = asyncio.run(evaluate(None, PRINCIPAL, [EvaluationCase("question", ("match",), ("public",))],
                                  k=1, retrievers={"dense": dense, "hybrid": hybrid}, run_kind="contract"))
    assert report["modes"]["dense"]["metrics"]["recall_at_k"] == 0
    assert report["modes"]["hybrid"]["metrics"]["recall_at_k"] == 1
    assert [item[0] for item in called] == ["dense", "hybrid"]
    assert all(item[1:] == (PRINCIPAL, ("public",), 1) for item in called)


def test_answerable_failures_count_zero_but_noanswer_failures_are_not_successful_abstention():
    async def failure(service, principal, case, k):
        raise ModelError("private-question SECRET API KEY")

    report = asyncio.run(evaluate(None, PRINCIPAL, [EvaluationCase("positive", ("match",)),
                                                   EvaluationCase("negative", ())],
                                  retrievers={"reranked": failure}, run_kind="contract"))
    metrics = report["modes"]["reranked"]["metrics"]
    assert metrics["recall_at_k"] == metrics["mrr_at_k"] == 0
    assert metrics["noanswer_falsepositive"] is None
    assert metrics["noanswer_errors"] == 1
    assert metrics["error_rate"] == 1
    assert "SECRET" not in json.dumps(report)
    assert report["modes"]["reranked"]["cases"][0]["error_code"] == "model_error"


def test_latency_p50_p95_interpolation_is_explicit(monkeypatch):
    ticks = iter([0., 1., 1., 4., 4., 9., 9., 16.])
    monkeypatch.setattr(evaluation.time, "perf_counter", lambda: next(ticks))

    async def contract_retriever(service, principal, case, k):
        return []

    report = asyncio.run(evaluate(None, PRINCIPAL, [EvaluationCase("q", ())] * 4,
                                  retrievers={"dense": contract_retriever}, run_kind="contract"))
    metrics = report["modes"]["dense"]["metrics"]
    assert metrics["latency_p50_ms"] == 4000
    assert metrics["latency_p95_ms"] == pytest.approx(6700)


def test_timeout_is_recorded_and_cancellation_propagates():
    async def slow(service, principal, case, k):
        await asyncio.sleep(.05)
        return []

    report = asyncio.run(evaluate(None, PRINCIPAL, [EvaluationCase("q", ())], timeout=.001,
                                  retrievers={"dense": slow}, run_kind="contract"))
    assert report["modes"]["dense"]["cases"][0]["error_code"] == "timeout"

    async def cancelled(service, principal, case, k):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(evaluate(None, PRINCIPAL, [EvaluationCase("q", ())], retrievers={"dense": cancelled}))


def test_out_of_scope_case_rejected_before_any_service_call():
    class NeverCalled:
        async def search(self, *args, **kwargs):
            pytest.fail("must reject scope before searching")

    with pytest.raises(AccessDenied):
        asyncio.run(evaluate(NeverCalled(), PRINCIPAL, [EvaluationCase("q", (), ("private",))]))


@pytest.mark.parametrize("output, error", [
    ([hit("doc", "secret")], "access_denied"),
    ({"hits": [{"document_id": "doc", "collection_id": "public", "score": float("nan")}]}, "invalid_result"),
    ({"hits": [{"text": "private-document"}]}, "invalid_result"),
    ({"hits": "bad"}, "invalid_result"),
    ({"data": []}, "invalid_result"),
])
def test_invalid_results_and_access_leaks_are_explicit_failures(output, error):
    async def contract_retriever(service, principal, case, k):
        return output

    report = asyncio.run(evaluate(None, PRINCIPAL, [EvaluationCase("q", ("doc",))],
                                  retrievers={"hybrid": contract_retriever}, run_kind="contract"))
    row = report["modes"]["hybrid"]["cases"][0]
    assert row["error_code"] == error
    assert row["returned_count"] == 0
    assert row["recall_at_k"] == 0


def test_top_k_bound_and_multiple_expected_documents():
    async def contract_retriever(service, principal, case, k):
        return [hit("irrelevant"), hit("match"), hit("another")]

    report = asyncio.run(evaluate(None, PRINCIPAL, [EvaluationCase("q", ("match", "another", "match"))],
                                  k=2, retrievers={"hybrid": contract_retriever}, run_kind="contract"))
    row = report["modes"]["hybrid"]["cases"][0]
    assert row["expected_count"] == 2
    assert row["returned_count"] == 2
    assert row["recall_at_k"] == row["reciprocal_rank"] == .5


def test_empty_evaluation_has_no_fabricated_metrics():
    report = asyncio.run(evaluate(None, PRINCIPAL, [], run_kind="contract"))
    metrics = report["modes"]["deployed"]["metrics"]
    assert metrics["cases"] == 0
    for key in ["recall_at_k", "mrr_at_k", "noanswer_falsepositive", "latency_p95_ms", "error_rate"]:
        assert metrics[key] is None


@pytest.mark.parametrize("case", [
    {"query": "q"}, {"query": "", "expected_document_ids": []},
    {"query": "q", "expected_document_ids": "doc"},
    {"query": "q", "expected_document_ids": [], "collections": []},
    {"query": "q", "expected_document_ids": [], "collection_ids": [], "collectionIDs": []},
])
def test_bad_cases_fail_before_running(case):
    with pytest.raises(EvaluationError):
        asyncio.run(evaluate(None, PRINCIPAL, [case]))


@pytest.mark.parametrize("options", [
    {"k": 0}, {"k": True}, {"timeout": float("nan")}, {"timeout": 0},
    {"run_kind": "quality-certified"}, {"retrievers": {}},
    {"retrievers": {"leaked-private-query": lambda *args: []}},
])
def test_bad_configuration_fails(options):
    with pytest.raises(EvaluationError):
        asyncio.run(evaluate(None, PRINCIPAL, [], **options))


def test_save_report_strips_injected_debug_fields_and_does_not_overwrite(tmp_path):
    async def contract_retriever(service, principal, case, k):
        return [hit("doc")]

    report = asyncio.run(evaluate(None, PRINCIPAL, [EvaluationCase("private-query", ("doc",))],
                                  retrievers={"dense": contract_retriever}, run_kind="contract"))
    report["debug"] = "SECRET-TEXT"
    report["generated_at"] = "SECRET-TEXT"
    report["modes"]["dense"]["metrics"]["source"] = "SECRET-TEXT"
    report["modes"]["dense"]["cases"][0]["raw_document"] = "SECRET-TEXT"
    output = save_report(report, tmp_path / "contract-report.json")
    text = output.read_text(encoding="utf-8")
    assert "SECRET-TEXT" not in text and "private-" not in text
    parsed = json.loads(text)
    assert parsed["run_kind"] == "contract"
    assert parsed["quality_verified"] is False
    with pytest.raises(FileExistsError):
        save_report(report, output)
    assert output.read_text(encoding="utf-8") == text


def test_save_rejects_private_exception_in_error_code(tmp_path):
    report = asyncio.run(evaluate(None, PRINCIPAL, [EvaluationCase("q", ())], run_kind="contract"))
    report["modes"]["deployed"]["cases"][0]["error_code"] = "SECRET-TEXT"
    with pytest.raises(EvaluationError):
        save_report(report, tmp_path / "not-written.json")
    assert not (tmp_path / "not-written.json").exists()
