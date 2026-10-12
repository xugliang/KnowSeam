import asyncio
import importlib.util
import json
import time

import pytest

from test_graph_service import search_system as _search_system

search_system = _search_system


def telemetry():
    assert importlib.util.find_spec("knowseam.observability"), "bounded retrieval tracing is missing"
    from knowseam import observability

    return observability


@pytest.mark.asyncio
async def test_trace_success_stage_order(search_system):
    o = telemetry()
    captured = []

    class Sink(o.Observer):
        async def emit(self, trace):
            captured.append(trace)

    search_system.service.observer = Sink()
    result = await search_system.service.search(search_system.principal, "PRIVATE-QUERY", ["public"], 5)
    assert "diagnostics" not in result
    trace = captured[0]
    phases = [m.phase for m in trace.stages]
    assert phases == ["auth", "embed", "tokenize", "retrieve", "graph", "fusion", "rerank", "revalidate"]
    assert all(m.duration_ms >= 0 for m in trace.stages)
    assert trace.status == "ok"
    assert len(trace.request_id) == 32
    text = json.dumps(trace.as_dict())
    for secret in ["PRIVATE-QUERY", "alice", "https://", "Approved documents", "password"]:
        assert secret not in text


@pytest.mark.asyncio
async def test_trace_sink_failure_does_not_fail_search(search_system):
    o = telemetry()

    class Broken(o.Observer):
        async def emit(self, trace):
            raise RuntimeError("SECRET FROM SINK")

    search_system.service.observer = Broken()
    result = await search_system.service.search(search_system.principal, "query", ["public"], 5)
    assert result["hits"]


@pytest.mark.asyncio
async def test_trace_sink_timeout_is_bounded(search_system):
    o = telemetry()

    class Slow(o.Observer):
        async def emit(self, trace):
            await asyncio.sleep(10)

    search_system.service.observer = Slow()
    result = await asyncio.wait_for(
        search_system.service.search(search_system.principal, "query", ["public"], 5), 1
    )
    assert result["hits"]


def test_trace_fixed_fields_no_hidden_candidates():
    o = telemetry()
    with o.trace_scope() as trace:
        with o.stage("dense"):
            pass
        with pytest.raises(ValueError):
            with o.stage("SECRET query"):
                pass
    assert "candidate_count" not in json.dumps(trace.as_dict())


@pytest.mark.asyncio
async def test_logging_handler_exceptions_never_escape(search_system, monkeypatch):
    o = telemetry()

    def broken(*args, **kwargs):
        raise RuntimeError("handler failure")

    monkeypatch.setattr(o.LOGGER, "info", broken)
    monkeypatch.setattr(o.LOGGER, "warning", broken)
    search_system.service.observer = o.LoggingObserver()
    assert (await search_system.service.search(search_system.principal, "q", ["public"], 5))["hits"]


@pytest.mark.asyncio
async def test_synchronous_sink_cannot_block_query_event_loop(search_system):
    o = telemetry()

    class Blocking(o.Observer):
        async def emit(self, trace):
            time.sleep(0.25)

    search_system.service.observer = Blocking()
    start = time.perf_counter()
    await search_system.service.search(search_system.principal, "q", ["public"], 5)
    assert time.perf_counter() - start < 0.15
