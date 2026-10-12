"""Actual isolated PostgreSQL and optional Neo4j; never production database writes."""
# ruff: noqa: F811 -- imported pytest fixtures intentionally match parameter names.
from dataclasses import replace
from datetime import datetime, timezone
import os

# Reuse the production-sync fixture and its strict knowseam_test identity gate.
from test_postgres_e2e import case, checked_dsn, tokenizer, run  # noqa: F401
from knowseam.storage import PostgresStore


def enable_projection(case):
    case.store = PostgresStore(checked_dsn_value(case), graph_projections={case.collection: "projection-v1"})
    case.worker.store = case.store
    case.service.store = case.store
    case.service.retrieval = case.store
    return case.store


def checked_dsn_value(case):
    return os.environ["SUITE_KNOWLEDGE_TEST_DSN"]


def claim(case):
    return case.store.graph_claim(case.company, {case.collection: "projection-v1"}, lease_seconds=600,
                                 max_attempts=3)


def test_publication_outbox_is_atomic_and_off_mode_has_no_events(case):
    case.write("A supports B.")
    case.sync()
    with case.store.connect() as c:
        assert c.execute("SELECT count(*) AS n FROM sk_graph_jobs WHERE company_id=%s", (case.company,)).fetchone()["n"] == 0
    enable_projection(case)
    case.write("A supports B and C.")
    case.sync()
    job = claim(case)
    assert job["action"] == "upsert"
    snapshot = case.store.graph_load(job)
    assert snapshot.chunks[0].text == "A supports B and C."
    assert snapshot.version_id == case.document()["active_version"]
    assert claim(case) is None


def test_outbox_refuses_expired_lease_and_limits_failed_attempts(case):
    enable_projection(case)
    case.write("A supports B.")
    case.sync()
    first = claim(case)
    with case.store.connect() as c:
        c.execute("UPDATE sk_graph_jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (first["id"],))
    assert not case.store.graph_complete(first)
    second = claim(case)
    assert second["claim_token"] != first["claim_token"]
    assert not case.store.graph_checkpoint(first, 0, {"entities": [], "relations": []})
    assert case.store.graph_fail(second, "TimeoutError", max_attempts=3, delay_seconds=0)
    third = claim(case)
    assert third["attempts"] == 3
    assert case.store.graph_fail(third, "TimeoutError", max_attempts=3, delay_seconds=0)
    assert claim(case) is None
    assert case.store.graph_status(case.company)[0]["state"] == "failed"


def test_update_obsoletes_old_event_and_delete_is_immediately_unreadable(case):
    from knowseam.graph.domain import EvidenceRef
    enable_projection(case)
    case.write("A supports B.")
    case.sync()
    old = claim(case)
    snapshot = case.store.graph_load(old)
    ref = snapshot.chunks[0].evidence
    assert case.store.graph_evidence(case.principal, [ref])[0].text == "A supports B."
    case.write("A supports C.")
    case.sync()
    assert case.store.graph_load(old) is None
    assert case.store.graph_evidence(case.principal, [ref]) == []
    assert case.store.graph_evidence(replace(case.principal, company_id="other"), [ref]) == []
    node = run(case.connector.scan()).nodes[0]
    case.store.observe(case.source, replace(node, public=False), datetime.now(timezone.utc))
    assert case.store.graph_evidence(case.principal, [EvidenceRef(**ref.model_dump())]) == []


def test_rebuild_queues_current_versions_without_reembedding(case):
    enable_projection(case)
    case.write("A supports B.")
    case.sync()
    calls = len(case.embedder.calls)
    assert case.store.graph_rebuild(case.company) == 1
    assert len(case.embedder.calls) == calls


def test_restoration_requeues_after_ready_transition_even_if_early_job_consumed(case):
    enable_projection(case)
    case.write("A supports B.")
    case.sync()
    initial = claim(case)
    assert case.store.graph_complete(initial)
    case.store.reconcile(case.source, [])
    deleted = claim(case)
    assert case.store.graph_complete(deleted)
    node = run(case.connector.scan()).nodes[0]
    case.store.observe(case.source, node, datetime.now(timezone.utc))
    early = claim(case)
    if early is not None:
        assert case.store.graph_load(early) is None
        assert case.store.graph_complete(early, obsolete=True)
    case.store.touch_unchanged(case.document()["id"], node.revision)
    restored = claim(case)
    assert restored is not None
    assert case.store.graph_load(restored).chunks[0].text == "A supports B."


def test_fingerprint_change_and_enable_schedule_once_without_reembedding(case):
    case.sync()
    enable_projection(case)
    calls = len(case.embedder.calls)
    assert case.store.graph_ensure(case.company) == 1
    assert case.store.graph_ensure(case.company) == 0
    first = claim(case)
    assert case.store.graph_complete(first)
    case.store.graph_projections = {case.collection: "projection-v2"}
    assert case.store.graph_ensure(case.company) == 1
    assert case.store.graph_ensure(case.company) == 0
    assert len(case.embedder.calls) == calls


def test_outbox_failure_rolls_back_document_publication(case):
    case.sync()
    previous = case.document()["active_version"]
    class BrokenOutbox(PostgresStore):
        def _enqueue_graph(self, connection, doc_id):
            raise RuntimeError("synthetic outbox failure")
    case.store = BrokenOutbox(checked_dsn_value(case), graph_projections={case.collection: "v1"})
    case.worker.store = case.store
    case.write("New version must not be published without an event.")
    assert case.sync()["failed"] == 1
    assert case.document()["active_version"] == previous


def test_graph_evidence_obeys_department_and_named_employee_permissions(case):
    enable_projection(case)
    case.write("A supports B.")
    case.sync()
    references = [chunk.evidence for chunk in case.store.graph_load(claim(case)).chunks]
    bob = replace(case.principal, actor_id="employee-b")
    def policy(visibility, **grants):
        case.store.configure(company_id=case.company,
            collections=[{"id": case.collection, "title": "Restricted", "visibility": visibility, **grants}],
            sources=[{"id": case.source, "collection_id": case.collection, "config_hash": case.config_hash}],
            employees=[{"actor_id": "employee-a", "departments": ["rnd"]},
                       {"actor_id": "employee-b", "departments": ["sales"]}])
    policy("department", departments=["rnd"])
    assert case.store.graph_evidence(case.principal, references)
    assert case.store.graph_evidence(bob, references) == []
    policy("employee", employees=["employee-b"])
    assert case.store.graph_evidence(case.principal, references) == []
    assert case.store.graph_evidence(bob, references)


def test_old_worker_cannot_consume_new_fingerprint_jobs(case):
    enable_projection(case)
    case.write("A supports B.")
    case.sync()
    old = claim(case)
    assert case.store.graph_complete(old)
    newer = PostgresStore(checked_dsn_value(case), graph_projections={case.collection: "projection-v2"})
    assert newer.graph_ensure(case.company) == 1
    assert claim(case) is None, "old worker stole a new projection and would mark it obsolete"
    job = newer.graph_claim(case.company, newer.graph_projections, lease_seconds=600, max_attempts=3)
    assert job["projection_key"] == "projection-v2"
    assert newer.graph_load(job) is not None
