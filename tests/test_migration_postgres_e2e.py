"""Checksummed migration replay and rollback on a strictly isolated real PG database."""

import uuid

import psycopg
import pytest

from test_postgres_e2e import checked_dsn as _checked_dsn

from knowseam.storage import PostgresStore
from knowseam.migrations.runner import Migration, MigrationError, MigrationRunner

checked_dsn = _checked_dsn  # fixture performs database identity guard


def test_real_migration_readonly_plan_replay_and_failed_step_rollback(checked_dsn):
    store = PostgresStore(checked_dsn)
    store.initialize()
    runner = MigrationRunner(store.connect)
    with store.connect() as connection:
        before = connection.execute("SELECT to_regclass('sk_schema_migrations') AS name").fetchone()["name"]
    plan = runner.plan()
    with store.connect() as connection:
        after = connection.execute("SELECT to_regclass('sk_schema_migrations') AS name").fetchone()["name"]
    assert before == after
    assert plan
    assert all(step.status == "applied" for step in runner.apply())
    assert runner.apply() == runner.plan()
    with pytest.raises(MigrationError, match="checksum"):
        altered = (Migration(runner.migrations[0].migration_id, "SELECT 1;"), *runner.migrations[1:])
        MigrationRunner(store.connect, altered).apply()
    table = "ks_test_rollback_" + uuid.uuid4().hex
    failing = Migration(
        f"{len(runner.migrations) + 1:03}_rollback", f"CREATE TABLE {table} (n integer); SELECT 1/0;"
    )
    with pytest.raises(psycopg.errors.DivisionByZero):
        MigrationRunner(store.connect, (*runner.migrations, failing)).apply()
    with store.connect() as connection:
        assert connection.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()["name"] is None
        assert (
            connection.execute(
                "SELECT 1 FROM sk_schema_migrations WHERE id=%s", (failing.migration_id,)
            ).fetchone()
            is None
        )
