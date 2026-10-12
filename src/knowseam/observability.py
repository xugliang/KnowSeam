"""Bounded, content-free retrieval diagnostics; no external telemetry dependency."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
import queue
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field

_REQUEST: ContextVar[str | None] = ContextVar('knowledge_request_id', default=None)
_TRACE: ContextVar[RetrievalTrace | None] = ContextVar('knowledge_trace', default=None)
_PHASES = frozenset({'auth', 'embed', 'tokenize', 'retrieve', 'dense', 'lexical', 'graph',
                     'fusion', 'rerank', 'coverage', 'revalidate'})
LOGGER = logging.getLogger('knowseam.retrieval')


def request_id() -> str:
    return _REQUEST.get() or uuid.uuid4().hex


@contextmanager
def request_scope():
    value = uuid.uuid4().hex  # Never trust a client-supplied correlation ID.
    token = _REQUEST.set(value)
    try:
        yield value
    finally:
        _REQUEST.reset(token)


@dataclass(frozen=True)
class StageMetric:
    phase: str
    duration_ms: float
    status: str


@dataclass
class RetrievalTrace:
    request_id: str
    stages: list[StageMetric] = field(default_factory=list)
    status: str = 'ok'
    returned_count: int = 0
    candidate_budget: int = 0
    degradation: str = 'none'

    def as_dict(self) -> dict:
        return asdict(self)


@contextmanager
def trace_scope():
    trace = RetrievalTrace(request_id())
    token = _TRACE.set(trace)
    try:
        yield trace
    except BaseException:
        trace.status = 'error'
        raise
    finally:
        _TRACE.reset(token)


@contextmanager
def stage(name: str):
    if name not in _PHASES:
        raise ValueError('invalid diagnostic phase')
    trace = _TRACE.get()
    started = time.perf_counter()
    status = 'ok'
    try:
        yield
    except BaseException:
        status = 'error'
        raise
    finally:
        if trace is not None and len(trace.stages) < 32:
            trace.stages.append(StageMetric(name, max(0, (time.perf_counter() - started) * 1000), status))


class Observer(ABC):
    """Runs on a bounded diagnostic worker, never on the query event loop.

    Sinks must create their I/O resources on their own execution loop and cannot
    depend on request-local identity. They receive only the completed safe trace.
    """
    @abstractmethod
    async def emit(self, trace: RetrievalTrace) -> None: ...


class NoOpObserver(Observer):
    async def emit(self, trace: RetrievalTrace) -> None:
        pass


class LoggingObserver(Observer):
    async def emit(self, trace: RetrievalTrace) -> None:
        import json
        LOGGER.info('retrieval_trace %s', json.dumps(trace.as_dict(), allow_nan=False))


_DELIVERIES: queue.Queue = queue.Queue(maxsize=32)
_WORKER_LOCK = threading.Lock()
_WORKER = None


def _consume():
    async def deliver(observer, trace):
        await asyncio.wait_for(observer.emit(trace), timeout=0.05)
    while True:
        observer, trace, loop, completed = _DELIVERIES.get()
        try:
            asyncio.run(deliver(observer, trace))
        except BaseException:
            # A failed logging handler must not recursively log its own failure.
            pass
        finally:
            def finish(future=completed):
                if not future.done():
                    future.set_result(None)
            try:
                loop.call_soon_threadsafe(finish)
            except RuntimeError:  # Request loop already shut down.
                pass
            _DELIVERIES.task_done()


async def emit_safely(observer: Observer, trace: RetrievalTrace) -> None:
    if type(observer) is NoOpObserver:
        return
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = threading.Thread(target=_consume, name='knowseam-diagnostics', daemon=True)
            _WORKER.start()
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    try:
        _DELIVERIES.put_nowait((observer, trace, loop, completed))
        await asyncio.wait_for(completed, timeout=0.05)
    except (queue.Full, TimeoutError):
        # At most one daemon and 32 pending safe traces. A hung third-party sink
        # drops diagnostics, never consumes unbounded threads or delays queries.
        pass
