import importlib.util
from pathlib import Path
import pytest


def module():
    path = Path(__file__).parents[1] / 'scripts' / 'benchmark_retrieval.py'
    spec = importlib.util.spec_from_file_location('benchmark_retrieval', path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_benchmark_refuses_non_test_database_and_fixes_resource_bounds():
    benchmark = module()
    with pytest.raises(ValueError):
        benchmark.checked_store('postgresql://localhost/production')
    with pytest.raises(ValueError):
        benchmark.checked_size(1000000)
    assert benchmark.checked_size(100000) == 100000
    assert benchmark.summary([1,2,3,4,5]) == {'p50_ms':3,'p95_ms':5}
