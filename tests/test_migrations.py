"""Migration safety: checksum validation, read-only planning and transactional resume."""
import importlib.util

import pytest


def runner_module():
    assert importlib.util.find_spec("knowseam.migrations") is not None, "explicit migrations missing"
    from knowseam.migrations import runner
    return runner


def test_migration_resume_and_checksum_mismatch():
    runner = runner_module()
    steps = (runner.Migration("001", "SELECT 1;"), runner.Migration("002", "SELECT 2;"))
    plan = runner.plan_migrations(steps, {"001": steps[0].checksum})
    assert [(row.migration_id, row.status) for row in plan] == [("001", "applied"), ("002", "pending")]
    with pytest.raises(runner.MigrationError, match="checksum"):
        runner.plan_migrations(steps, {"001": "altered"})


def test_migration_unknown_or_duplicate_history_rejected():
    runner = runner_module()
    one = runner.Migration("001", "SELECT 1;")
    with pytest.raises(runner.MigrationError):
        runner.plan_migrations((one, one), {})
    with pytest.raises(runner.MigrationError):
        runner.plan_migrations((one,), {"999": "old"})


def test_packaged_schema_can_be_read_without_models():
    runner = runner_module()
    steps = runner.packaged_migrations()
    assert steps[0].migration_id == "001_structure"
    assert all(item.checksum and item.sql.strip() for item in steps)


@pytest.mark.parametrize('ids', [('001_a', '003_b'), ('001_a', '001_b')])
def test_migration_sequence_cannot_skip_or_reuse_number(ids):
    runner = runner_module()
    with pytest.raises(runner.MigrationError):
        runner.plan_migrations(tuple(runner.Migration(value, 'SELECT 1;') for value in ids), {})
