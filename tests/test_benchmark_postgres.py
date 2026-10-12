import json
import os
from pathlib import Path
import pytest
from test_benchmark_retrieval import module
from test_postgres_e2e import checked_dsn as _checked_dsn

checked_dsn = _checked_dsn


def test_isolated_retrieval_benchmark(checked_dsn):
    raw = os.environ.get("KNOWSEAM_BENCHMARK_SIZE")
    if raw is None:
        pytest.skip("opt-in synthetic performance run, not ordinary regression")
    benchmark = module()
    size = benchmark.checked_size(int(raw))
    report = benchmark.benchmark(benchmark.checked_store(checked_dsn), size)
    output = Path(".build-three-phases/benchmarks")
    output.mkdir(parents=True, exist_ok=True)
    (output / f"synthetic-{size}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert all(s["ranking_equal"] for s in report["scenarios"])
