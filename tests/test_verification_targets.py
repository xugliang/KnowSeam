"""The isolated DB runner may run project tests, not arbitrary filesystem targets."""
from pathlib import Path
import runpy

import pytest


def test_isolated_runner_target_bounds(tmp_path):
    runner = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify_postgres.py"))
    assert "checked_targets" in runner, "test runner cannot select new integration tests safely"
    check = runner["checked_targets"]
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_one.py").touch()
    assert check(tmp_path, ["tests/test_one.py"]) == ["tests/test_one.py"]
    for bad in (["../secret"], ["--help"], ["src"], ["tests/../file"], []):
        with pytest.raises(ValueError):
            check(tmp_path, bad)
