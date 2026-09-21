"""Offline retrieval evaluation; company queries never appear in saved reports.

No telemetry/upload, model download, LLM judge, or generated ground truth is
performed. A callback may exercise dense/hybrid/reranked variants without coupling
the evaluator to a specific store or model server. Contract doubles only verify
the calculation, not actual retrieval quality.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .domain import AccessDenied, KnowledgeError, Principal, SearchHit
from .models import ModelError

SCHEMA_VERSION = "knowseam-evaluation-v1"
MODES = frozenset({"dense", "hybrid", "reranked", "deployed"})
RUN_KINDS = frozenset({"contract", "real", "unverified"})
ERROR_CODES = frozenset({"timeout", "access_denied", "model_error", "invalid_result", "service_error"})


class EvaluationError(KnowledgeError):
    pass


def _ids(values: Any, *, maximum: int) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or len(values) > maximum:
        raise EvaluationError("case identifiers must be a bounded sequence")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in values):
        raise EvaluationError("case identifiers must be nonempty strings of at most 256 characters")
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class EvaluationCase:
    query: str
    expected_document_ids: tuple[str, ...]
    collection_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 8192:
            raise EvaluationError("case query must be nonempty and at most 8192 characters")
        object.__setattr__(self, "expected_document_ids", _ids(self.expected_document_ids, maximum=1000))
        object.__setattr__(self, "collection_ids", _ids(self.collection_ids, maximum=128))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EvaluationCase:
        allowed = {"query", "expected_document_ids", "collection_ids", "collectionIDs"}
        if set(value) - allowed or "query" not in value or "expected_document_ids" not in value:
            raise EvaluationError("case requires query and expected_document_ids; unknown fields are rejected")
        if "collection_ids" in value and "collectionIDs" in value:
            raise EvaluationError("use only one spelling of collection_ids")
        return cls(
            query=value["query"], expected_document_ids=value["expected_document_ids"],
            collection_ids=value.get("collection_ids", value.get("collectionIDs", ())),
        )


Retriever = Callable[[Any, Principal, EvaluationCase, int], Awaitable[Any]]


async def _default_retriever(service: Any, principal: Principal, case: EvaluationCase, k: int) -> Any:
    return await service.search(principal, case.query, collection_ids=case.collection_ids, limit=k)


def _result_ids(result: Any, principal: Principal, case: EvaluationCase, k: int) -> list[str]:
    # Service API shape is {"hits": [asdict(SearchHit), ...]}; raw sequences are
    # accepted for low-level retriever callbacks and in-process SearchHit objects.
    hits = result.get("hits") if isinstance(result, Mapping) else result
    if isinstance(hits, (str, bytes)) or not isinstance(hits, Sequence) or len(hits) > 1000:
        raise EvaluationError("retriever must return a bounded sequence of SearchHit results")
    allowed = set(principal.allowed_collections)
    scope = set(case.collection_ids) or allowed
    documents = []
    for hit in hits:
        if isinstance(hit, SearchHit):
            document_id, collection_id, score = hit.document_id, hit.collection_id, hit.score
        elif isinstance(hit, Mapping):
            document_id, collection_id, score = hit.get("document_id"), hit.get("collection_id"), hit.get("score")
        else:
            raise EvaluationError("invalid search result type")
        if not isinstance(document_id, str) or not document_id or len(document_id) > 256:
            raise EvaluationError("search result is missing a valid document identity")
        if not isinstance(collection_id, str) or collection_id not in allowed or collection_id not in scope:
            raise AccessDenied("evaluation received a result outside the authenticated collection scope")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise EvaluationError("search result score must be finite")
        documents.append(document_id)
    return documents[:k]


def _percentile(values: list[float], percentile: float) -> float | None:
    """Linear interpolation between sorted samples, identical for one-element sets."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in rows if row["expected_count"] > 0]
    noanswer = [row for row in rows if row["expected_count"] == 0]
    evaluated_noanswer = [row for row in noanswer if row["status"] == "ok"]
    errors = sum(row["status"] == "error" for row in rows)
    # Failed answerable requests count as zero recall/RR; failures must not make
    # a weak deployment look better by silently removing its difficult queries.
    return {
        "cases": len(rows),
        "answerable_cases": len(answerable),
        "noanswer_cases": len(noanswer),
        "errors": errors,
        "error_rate": errors / len(rows) if rows else None,
        "recall_at_k": sum(row["recall_at_k"] for row in answerable) / len(answerable) if answerable else None,
        "mrr_at_k": sum(row["reciprocal_rank"] for row in answerable) / len(answerable) if answerable else None,
        "noanswer_evaluated": len(evaluated_noanswer),
        "noanswer_errors": len(noanswer) - len(evaluated_noanswer),
        "noanswer_falsepositive": (
            sum(bool(row["returned_count"]) for row in evaluated_noanswer) / len(evaluated_noanswer)
            if evaluated_noanswer else None
        ),
        "latency_p50_ms": _percentile([row["latency_ms"] for row in rows], .5),
        "latency_p95_ms": _percentile([row["latency_ms"] for row in rows], .95),
    }


async def evaluate(
    service: Any, principal: Principal,
    cases: Sequence[EvaluationCase | Mapping[str, Any]], *, k: int = 10,
    retrievers: Mapping[str, Retriever] | None = None, timeout: float = 60,
    run_kind: str = "unverified",
) -> dict[str, Any]:
    """Evaluate real service retrieval or explicitly labeled contract callbacks.

    Modes are sequential to avoid creating unapproved concurrent model workloads.
    The caller owns model readiness, ground truth and source/collection access.
    Reports intentionally contain no queries, source URLs, IDs, texts or secrets.
    """
    if not isinstance(principal, Principal):
        raise EvaluationError("a trusted Principal is required")
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 100:
        raise EvaluationError("k must be an integer between 1 and 100")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 600):
        raise EvaluationError("timeout must be in (0, 600] seconds")
    if run_kind not in RUN_KINDS:
        raise EvaluationError("run_kind must be contract, real or unverified")
    if isinstance(cases, (str, bytes)) or not isinstance(cases, Sequence) or len(cases) > 10_000:
        raise EvaluationError("cases must be a sequence of at most 10000 items")
    parsed = []
    for value in cases:
        if isinstance(value, EvaluationCase):
            case = value
        elif isinstance(value, Mapping):
            case = EvaluationCase.from_mapping(value)
        else:
            raise EvaluationError("invalid evaluation case")
        if not set(case.collection_ids).issubset(principal.allowed_collections):
            raise AccessDenied("evaluation requests an unauthorized collection")
        parsed.append(case)
    configured = {"deployed": _default_retriever} if retrievers is None else dict(retrievers)
    if not configured or set(configured) - MODES or not all(callable(value) for value in configured.values()):
        raise EvaluationError("retrievers must map dense/hybrid/reranked/deployed to async callbacks")

    results = {}
    for mode, callback in configured.items():
        rows = []
        for index, case in enumerate(parsed, start=1):
            started = time.perf_counter()
            error_code = None
            ids: list[str] = []
            try:
                raw = await asyncio.wait_for(callback(service, principal, case, k), timeout=timeout)
                ids = _result_ids(raw, principal, case, k)
            except TimeoutError:
                error_code = "timeout"
            except AccessDenied:
                error_code = "access_denied"
            except ModelError:
                error_code = "model_error"
            except EvaluationError:
                error_code = "invalid_result"
            except Exception:
                # Never save or log exception bodies; upstream errors can echo
                # company queries, document contents or Authorization headers.
                error_code = "service_error"
            elapsed = max(0.0, (time.perf_counter() - started) * 1000)
            expected = set(case.expected_document_ids)
            matched = len(expected.intersection(ids))
            first_rank = next((rank for rank, doc_id in enumerate(ids, 1) if doc_id in expected), None)
            rows.append({
                "case_index": index,
                "status": "error" if error_code else "ok",
                "error_code": error_code,
                "expected_count": len(expected),
                "returned_count": len(ids),
                "matched_document_count": matched,
                "recall_at_k": matched / len(expected) if expected else None,
                "reciprocal_rank": 1 / first_rank if first_rank else 0.0,
                "latency_ms": elapsed,
            })
        results[mode] = {"metrics": _metrics(rows), "cases": rows}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_kind": run_kind,
        "quality_verified": False,
        "k": k,
        "ranking_unit": "retrieved_chunks_with_document_relevance",
        "modes": results,
    }


def _sanitized_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist saved fields even if a caller adds raw debug data to the report."""
    if report.get("schema_version") != SCHEMA_VERSION or report.get("run_kind") not in RUN_KINDS:
        raise EvaluationError("unsupported evaluation report")
    if isinstance(report.get("k"), bool) or not isinstance(report.get("k"), int) or not 1 <= report["k"] <= 100:
        raise EvaluationError("invalid report k")
    modes = report.get("modes")
    if not isinstance(modes, Mapping) or not modes or set(modes) - MODES:
        raise EvaluationError("invalid report modes")
    cleaned_modes = {}
    numeric_fields = (
        "case_index", "expected_count", "returned_count", "matched_document_count",
        "recall_at_k", "reciprocal_rank", "latency_ms",
    )
    for mode, result in modes.items():
        if not isinstance(result, Mapping) or not isinstance(result.get("cases"), list):
            raise EvaluationError("invalid report cases")
        if len(result["cases"]) > 10000:
            raise EvaluationError("too many report cases")
        rows = []
        for row in result["cases"]:
            if not isinstance(row, Mapping) or row.get("status") not in {"ok", "error"}:
                raise EvaluationError("invalid report row")
            error_code = row.get("error_code")
            if error_code is not None and error_code not in ERROR_CODES:
                raise EvaluationError("invalid error code")
            clean = {"status": row["status"], "error_code": error_code}
            for field in numeric_fields:
                value = row.get(field)
                if field == "recall_at_k" and value is None:
                    clean[field] = None
                elif (isinstance(value, bool) or not isinstance(value, (int, float))
                      or not math.isfinite(value) or value < 0):
                    raise EvaluationError("report statistics must be finite nonnegative numbers")
                else:
                    clean[field] = value
            rows.append(clean)
        # Recompute the aggregation; free-form caller-supplied metrics are not saved.
        cleaned_modes[mode] = {"metrics": _metrics(rows), "cases": rows}
    return {
        "schema_version": SCHEMA_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "run_kind": report["run_kind"],
        "quality_verified": False,
        "k": report["k"],
        "ranking_unit": "retrieved_chunks_with_document_relevance",
        "modes": cleaned_modes,
    }


def save_report(report: Mapping[str, Any], path: str | Path) -> Path:
    """Write one new local JSON report; no overwrite, HTTP, telemetry or uploads."""
    cleaned = _sanitized_report(report)
    output = Path(path).resolve()
    payload = json.dumps(cleaned, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
    return output
