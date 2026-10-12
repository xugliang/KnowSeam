"""Checksummed migrations with read-only planning and a dedicated advisory lock."""
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import re
from typing import Callable, Mapping, Sequence


class MigrationError(ValueError):
    pass


@dataclass(frozen=True)
class Migration:
    migration_id: str
    sql: str

    def __post_init__(self):
        if not re.fullmatch(r"[0-9]{3}(?:_[a-z0-9_]+)?", self.migration_id) or not self.sql.strip():
            raise MigrationError("invalid_migration")

    @property
    def checksum(self) -> str:
        return sha256(self.sql.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MigrationStep:
    migration_id: str
    checksum: str
    status: str


def packaged_migrations() -> tuple[Migration, ...]:
    root = files("knowseam.migrations").joinpath("sql")
    return tuple(Migration(path.name[:-4], path.read_text(encoding="utf-8"))
                 for path in sorted(root.iterdir(), key=lambda item: item.name) if path.name.endswith(".sql"))


def plan_migrations(migrations: Sequence[Migration], applied: Mapping[str, str]) -> list[MigrationStep]:
    ids = [item.migration_id for item in migrations]
    numbers = [int(item[:3]) for item in ids]
    if (numbers != list(range(1, len(ids) + 1)) or len(ids) != len(set(ids))
            or ids != sorted(ids) or set(applied) - set(ids)):
        raise MigrationError("incompatible_migration_history")
    pending_seen = False
    result = []
    for item in migrations:
        checksum = applied.get(item.migration_id)
        if checksum is not None and checksum != item.checksum:
            raise MigrationError("migration_checksum_mismatch")
        if checksum is not None and pending_seen:
            raise MigrationError("migration_history_has_gap")
        pending_seen |= checksum is None
        result.append(MigrationStep(item.migration_id, item.checksum, "pending" if checksum is None else "applied"))
    return result


class MigrationRunner:
    def __init__(self, connect: Callable, migrations: Sequence[Migration] | None = None):
        self.connect = connect
        self.migrations = tuple(packaged_migrations() if migrations is None else migrations)
        plan_migrations(self.migrations, {})

    @staticmethod
    def _applied(connection) -> dict[str, str]:
        exists = connection.execute("SELECT to_regclass('sk_schema_migrations') AS name").fetchone()["name"]
        if not exists:
            return {}
        return {row["id"]: row["checksum"] for row in connection.execute(
            "SELECT id,checksum FROM sk_schema_migrations ORDER BY id").fetchall()}

    def plan(self) -> list[MigrationStep]:
        with self.connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            return plan_migrations(self.migrations, self._applied(connection))

    def apply(self) -> list[MigrationStep]:
        with self.connect() as connection:
            connection.autocommit = True
            connection.execute("SELECT pg_advisory_lock(hashtextextended('knowseam:schema-migrations',0))")
            try:
                plan_migrations(self.migrations, self._applied(connection))
                for migration in self.migrations:
                    with connection.transaction():
                        applied = self._applied(connection)
                        plan_migrations(self.migrations, applied)
                        if migration.migration_id in applied:
                            continue
                        connection.execute("CREATE TABLE IF NOT EXISTS sk_schema_migrations ("
                                           "id text PRIMARY KEY,checksum text NOT NULL,"
                                           "applied_at timestamptz NOT NULL DEFAULT now())")
                        connection.execute(migration.sql)
                        connection.execute("INSERT INTO sk_schema_migrations(id,checksum) VALUES(%s,%s)",
                                           (migration.migration_id, migration.checksum))
                return plan_migrations(self.migrations, self._applied(connection))
            finally:
                connection.execute("SELECT pg_advisory_unlock(hashtextextended('knowseam:schema-migrations',0))")
