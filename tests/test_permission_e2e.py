from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from test_postgres_e2e import case as _case, checked_dsn as _checked_dsn, tokenizer as _tokenizer, run

from knowseam.domain import AccessDenied, NotFound
from knowseam.migrations.runner import MigrationRunner

case, checked_dsn, tokenizer = _case, _checked_dsn, _tokenizer


def authority(case, mode="source_acl"):
    from knowseam.permissions.postgres import PermissionAuthority

    MigrationRunner(case.store.connect).apply()
    case.store.permissions = PermissionAuthority(case.store)
    case.store.configure(
        company_id=case.company,
        collections=[{"id": case.collection, "title": "test"}],
        employees=[{"actor_id": case.principal.actor_id, "active": True}],
        sources=[
            {
                "id": case.source,
                "collection_id": case.collection,
                "config_hash": case.config_hash,
                "permission_mode": mode,
                "bindings": [
                    {
                        "actor_id": case.principal.actor_id,
                        "kind": "user",
                        "subject_id": "upstream-user",
                        "valid_until": datetime.now(timezone.utc) + timedelta(days=1),
                    }
                ],
            }
        ],
    )
    return case.store.permissions


def test_private_grants_intersect_collection_and_never_set_public(case):
    from knowseam.permissions.domain import PermissionSnapshot, Grant

    case.sync()
    acl = authority(case)
    doc = case.document()
    obs = acl.begin(case.source)
    assert acl.apply(
        obs,
        PermissionSnapshot(
            external_id="guide.md",
            state="verified",
            complete=True,
            public=False,
            grants=(Grant(kind="user", subject_id="upstream-user"),),
        ),
    )
    assert not case.document()["public"]
    assert case.store.read(case.principal, doc["id"], doc["active_version"], 0, 20)["sections"]
    with pytest.raises(NotFound):
        case.store.read(replace(case.principal, actor_id="other"), doc["id"], doc["active_version"], 0, 20)
    # Old reader is fail-closed for private documents.
    from knowseam.storage import PostgresStore

    old = PostgresStore(case.store._dsn)
    with pytest.raises(NotFound):
        old.read(case.principal, doc["id"], doc["active_version"], 0, 20)


def test_delayed_scan_cannot_restore_newer_revocation_and_publish_revision(case):
    from knowseam.permissions.domain import PermissionSnapshot

    case.sync()
    acl = authority(case, "public_verified")
    old = acl.begin(case.source)
    fresh = acl.begin(case.source)
    assert acl.apply(
        fresh,
        PermissionSnapshot(external_id="guide.md", state="revoked", complete=True, public=False, grants=()),
    )
    node = run(case.connector.scan()).nodes[0]
    case.store.observe(case.source, node, old.started_at, _observation=old)
    assert not case.document()["public"]
    assert not acl.apply(
        old,
        PermissionSnapshot(external_id="guide.md", state="verified", complete=True, public=True, grants=()),
    )
    with pytest.raises(AccessDenied):
        case.store.touch_unchanged(case.document()["id"], node.revision, _permission_revision="old")


def test_partial_acl_revokes_access_without_refreshing_old_lease(case):
    from knowseam.permissions.domain import PermissionSnapshot

    case.sync()
    acl = authority(case, "public_verified")
    obs = acl.begin(case.source)
    acl.apply(
        obs,
        PermissionSnapshot(external_id="guide.md", state="verified", complete=True, public=True, grants=()),
    )
    doc = case.document()
    assert case.store.read(case.principal, doc["id"], doc["active_version"], 0, 20)
    acl.apply(acl.begin(case.source), PermissionSnapshot(external_id="guide.md"))
    with pytest.raises(NotFound):
        case.store.read(case.principal, doc["id"], doc["active_version"], 0, 20)


def test_permission_only_sync_works_while_content_lock_held_without_models(case, monkeypatch):
    from knowseam.permissions.worker import PermissionWorker

    case.sync()
    acl = authority(case, "public_verified")

    # The independent worker must not hash content, invoke a parser or wait on the content lock.
    async def forbidden(*args, **kwargs):
        pytest.fail("permission-only worker touched content/model")

    monkeypatch.setattr(case.connector, "scan", forbidden)
    monkeypatch.setattr(case.connector, "fetch", forbidden)
    monkeypatch.setattr(case.embedder, "embed", forbidden)
    with case.store.source_lock(case.source, case.config_hash):
        assert (
            run(PermissionWorker(acl).sync(case.source, case.connector, case.config_hash))[
                "permissions_applied"
            ]
            == 1
        )
        assert case.read()["sections"]
        case.connector.public_files = frozenset()
        run(PermissionWorker(acl).sync(case.source, case.connector, case.config_hash))
    with pytest.raises(NotFound):
        case.read()


def test_permission_enabled_content_sync_and_revision_fence(case):
    from knowseam.permissions.domain import PermissionSnapshot

    acl = authority(case, "public_verified")
    assert case.sync()["published"] == 1
    assert case.sync()["unchanged"] == 1
    with case.store.connect() as c:
        revision = acl.admission(c, case.document()["id"])
    acl.apply(
        acl.begin(case.source),
        PermissionSnapshot(external_id="guide.md", state="verified", public=True, complete=True, grants=()),
    )
    # Refreshing an identical effective ACL must not starve long content/model work.
    case.store.touch_unchanged(case.document()["id"], "revision", _permission_revision=revision)


def test_private_graph_admission_and_employee_disable_and_ttl(case):
    from knowseam.permissions.domain import PermissionSnapshot, Grant

    case.sync()
    acl = authority(case)
    acl.apply(
        acl.begin(case.source),
        PermissionSnapshot(
            external_id="guide.md",
            state="verified",
            complete=True,
            grants=(Grant(kind="user", subject_id="upstream-user"),),
        ),
    )
    case.store.graph_projections = {case.collection: "private-projection"}
    case.store.graph_rebuild(case.company)
    job = case.store.graph_claim(
        case.company, case.store.graph_projections, lease_seconds=600, max_attempts=3
    )
    assert job["action"] == "upsert" and case.store.graph_load(job).chunks
    assert not case.document()["public"]
    with case.store.connect() as c:
        c.execute("UPDATE sk_employees SET active=false WHERE company_id=%s", (case.company,))
    with pytest.raises(NotFound):
        case.read()
    with case.store.connect() as c:
        c.execute("UPDATE sk_employees SET active=true WHERE company_id=%s", (case.company,))
        c.execute(
            "UPDATE sk_document_permissions SET verified_at=now()-interval '1 day' WHERE document_id=%s",
            (case.document()["id"],),
        )
    assert case.store.graph_load(job) is None
    with pytest.raises(NotFound):
        case.read()


def test_plain_read_checks_acl_again_after_snapshot(case, monkeypatch):
    import knowseam.context_store as context

    case.sync()
    original = context.require_document

    def revoked(store, connection, principal, document_id, version_id):
        store.reconcile(case.source, [])
        return original(store, connection, principal, document_id, version_id)

    monkeypatch.setattr(context, "require_document", revoked)
    with pytest.raises(NotFound):
        case.read()


def test_old_connector_cannot_stamp_new_source_configuration(case, monkeypatch):
    from knowseam.permissions.worker import PermissionWorker

    case.sync()
    acl = authority(case, "public_verified")
    begin = acl.begin

    def changed(source_id, **kwargs):
        with case.store.connect() as c:
            c.execute("UPDATE sk_sources SET config_hash=%s WHERE id=%s", ("new-source", case.source))
        return begin(source_id, **kwargs)

    monkeypatch.setattr(acl, "begin", changed)
    with pytest.raises(AccessDenied):
        run(PermissionWorker(acl).sync(case.source, case.connector, case.config_hash))
    with case.store.connect() as c:
        assert (
            c.execute(
                "SELECT config_hash FROM sk_document_permissions WHERE document_id=%s",
                (case.document()["id"],),
            ).fetchone()["config_hash"]
            == case.config_hash
        )


def test_revoke_then_restore_does_not_reuse_old_publication_generation(case):
    from knowseam.permissions.domain import PermissionSnapshot

    case.sync()
    acl = authority(case, "public_verified")
    public = PermissionSnapshot(
        external_id="guide.md", state="verified", public=True, complete=True, grants=()
    )
    acl.apply(acl.begin(case.source), public)
    with case.store.connect() as c:
        first = acl.admission(c, case.document()["id"])
    acl.apply(
        acl.begin(case.source),
        PermissionSnapshot(external_id="guide.md", state="revoked", complete=True, grants=()),
    )
    acl.apply(acl.begin(case.source), public)
    with pytest.raises(AccessDenied):
        case.store.touch_unchanged(case.document()["id"], "revision", _permission_revision=first)
