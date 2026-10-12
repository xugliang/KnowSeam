from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest
from test_postgres_e2e import case as _case, checked_dsn as _checked_dsn, tokenizer as _tokenizer, run

from knowseam.domain import NotFound
from knowseam.graph.domain import ChunkExtraction
from knowseam.migrations.runner import MigrationRunner

case, checked_dsn, tokenizer = _case, _checked_dsn, _tokenizer


def setup(case):
    from knowseam.history.postgres import PostgresHistory

    MigrationRunner(case.store.connect).apply()
    case.store.history_enabled = True
    return PostgresHistory(case.store)


def test_old_read_rejects_inactive_history_uses_current_acl_and_known_time(case):
    from knowseam.history.domain import HistoryReadRequest, TemporalUnavailable

    history = setup(case)
    case.write("Original evidence")
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    case.sync()
    old = case.document()
    request = HistoryReadRequest(document_id=old["id"], version_id=old["active_version"])
    case.write("Changed evidence")
    case.sync()
    with pytest.raises(NotFound):
        case.store.read(case.principal, old["id"], old["active_version"], 0, 10)
    assert history.read_history(case.principal, request)["sections"][0]["text"] == "Original evidence"
    with pytest.raises(TemporalUnavailable, match="version_not_known_at_time"):
        history.read_history(case.principal, request.model_copy(update={"known_as_of": before}))
    with case.store.connect() as c:
        c.execute("UPDATE sk_documents SET public=false WHERE id=%s", (old["id"],))
    with pytest.raises(NotFound):
        history.read_history(case.principal, request)


def test_missing_metadata_not_backfilled_and_new_source_does_not_authorize_old_history(case):
    from knowseam.history.domain import HistoryReadRequest, TemporalUnavailable

    case.sync()
    old = case.document()
    history = setup(case)
    request = HistoryReadRequest(document_id=old["id"], version_id=old["active_version"])
    result = history.read_history(case.principal, request)
    assert result["metadata_status"] == "metadata_incomplete" and result["title"] is None
    with pytest.raises(TemporalUnavailable, match="temporal_unverifiable"):
        history.read_history(
            case.principal, request.model_copy(update={"known_as_of": datetime.now(timezone.utc)})
        )
    case.write("new evidence")
    case.sync()
    old = case.document()
    request = HistoryReadRequest(document_id=old["id"], version_id=old["active_version"])
    case.configure(config_hash="new-source")
    case.sync()
    with pytest.raises(NotFound):
        history.read_history(case.principal, request)


def test_late_facts_unknown_validity_idempotence_and_hidden_conflict(case):
    from knowseam.history.domain import HistorySearchRequest
    from knowseam.graph.facts import FactRecorder
    from knowseam.graph.domain import GraphOntology

    history = setup(case)
    ontology = GraphOntology(entity_types=("product",), relation_types=("price",))
    case.store.graph_projections = {case.collection: "facts-v1"}
    case.write("A costs B since 2020-01-01T00:00:00Z.\n\nA costs C.")
    case.sync()
    job = case.store.graph_claim(
        case.company, case.store.graph_projections, lease_seconds=600, max_attempts=3
    )
    snapshot = case.store.graph_load(job)
    values = []
    for label, chunk in zip(("B", "C"), snapshot.chunks):
        values.append(
            ChunkExtraction.model_validate(
                {
                    "entities": [
                        {"key": "a", "name": "A", "type": "product", "quote": chunk.text},
                        {"key": "b", "name": label, "type": "product", "quote": chunk.text},
                    ],
                    "relations": [
                        {
                            "subject": "a",
                            "object": "b",
                            "type": "price",
                            "quote": chunk.text,
                            **(
                                {
                                    "valid_from": "2020-01-01T00:00:00Z",
                                    "valid_from_quote": "2020-01-01T00:00:00Z",
                                }
                                if label == "B"
                                else {}
                            ),
                        }
                    ],
                }
            )
        )
    recorder = FactRecorder(case.store)
    snapshot = replace(snapshot, extractions=tuple(values))
    assert recorder.record(job, snapshot, ontology)
    assert recorder.record(job, snapshot, ontology)
    request = HistorySearchRequest(query="A", valid_as_of="2021-01-01T00:00:00Z")
    result = history.search_history(case.principal, request)
    assert len(result["observations"]) == 1
    assert result["observations"][0]["object"] == "B"
    assert (
        len(
            history.search_history(
                case.principal, request.model_copy(update={"include_unknown_validity": True})
            )["observations"]
        )
        == 2
    )
    assert (
        history.search_history(
            case.principal,
            request.model_copy(update={"known_as_of": datetime(2021, 1, 1, tzinfo=timezone.utc)}),
        )["observations"]
        == []
    )
    with case.store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM sk_fact_events WHERE company_id=%s", (case.company,)
            ).fetchone()["n"]
            == 1
        )
        c.execute("UPDATE sk_documents SET public=false WHERE id=%s", (case.document()["id"],))
    assert history.search_history(case.principal, request)["observations"] == []


def accept_fact(case, label, *, filename="guide.md"):
    from knowseam.graph.facts import FactRecorder
    from knowseam.graph.domain import GraphOntology

    case.store.graph_projections = {case.collection: "facts-v1"}
    case.write("A costs " + label + " since 2020-01-01T00:00:00Z.", filename)
    case.sync()
    job = case.store.graph_claim(
        case.company, case.store.graph_projections, lease_seconds=600, max_attempts=3
    )
    while job and job["document_id"] != case.document(filename)["id"]:
        case.store.graph_complete(job, obsolete=True)
        job = case.store.graph_claim(
            case.company, case.store.graph_projections, lease_seconds=600, max_attempts=3
        )
    snapshot = case.store.graph_load(job)
    text = snapshot.chunks[0].text
    value = ChunkExtraction.model_validate(
        {
            "entities": [
                {"key": "a", "name": "A", "type": "p", "quote": text},
                {"key": "b", "name": label, "type": "p", "quote": text},
            ],
            "relations": [
                {
                    "subject": "a",
                    "object": "b",
                    "type": "price",
                    "quote": text,
                    "valid_from": "2020-01-01T00:00:00Z",
                    "valid_from_quote": "2020-01-01T00:00:00Z",
                }
            ],
        }
    )
    assert FactRecorder(case.store).record(
        job,
        replace(snapshot, extractions=(value,)),
        GraphOntology(entity_types=("p",), relation_types=("price",)),
    )
    case.store.graph_complete(job)
    with case.store.connect() as c:
        return c.execute(
            "SELECT id FROM sk_fact_observations WHERE company_id=%s AND object=%s", (case.company, label)
        ).fetchone()["id"]


def test_explicit_correction_changes_known_interval_and_is_idempotent(case):
    from knowseam.history.domain import HistorySearchRequest
    from knowseam.graph.facts import FactRecorder

    history = setup(case)
    first = accept_fact(case, "B")
    second = accept_fact(case, "C")
    before = datetime.now(timezone.utc)
    recorder = FactRecorder(case.store)
    assert recorder.correct(case.company, first, second, event_id="review-1")
    assert recorder.correct(case.company, first, second, event_id="review-1")
    request = HistorySearchRequest(query="A", valid_as_of="2021-01-01T00:00:00Z")
    assert {x["object"] for x in history.search_history(case.principal, request)["observations"]} == {"C"}
    assert {
        x["object"]
        for x in history.search_history(case.principal, request.model_copy(update={"known_as_of": before}))[
            "observations"
        ]
    } == {"B", "C"}


def test_correction_allows_same_subject_identity_with_different_display_and_object_qualifier(case):
    from knowseam.graph.facts import FactRecorder

    setup(case)
    first = accept_fact(case, "B")
    second = accept_fact(case, "C")
    with case.store.connect() as c:
        c.execute(
            "UPDATE sk_fact_observations SET subject='a',qualifier='[\"\",\"east\"]' WHERE id=%s", (second,)
        )
    assert FactRecorder(case.store).correct(
        case.company, first, second, event_id="case-insensitive-correction"
    )


def test_unextracted_accepted_versions_are_reported_as_partial_not_no_facts(case):
    from knowseam.history.domain import HistorySearchRequest

    history = setup(case)
    case.write("Earlier product rule.")
    case.sync()
    case.write("Newer product rule.")
    case.sync()
    result = history.search_history(
        case.principal,
        HistorySearchRequest(
            query="product", valid_as_of="2026-01-01T00:00:00Z", include_unknown_validity=True
        ),
    )
    assert result["observations"] == []
    assert result["temporal_status"] == "partial"
    assert result["fact_coverage"] == "unextracted_versions"


def test_history_only_worker_does_not_require_neo4j(case):
    from test_derivation_lifecycle import setup as graph_setup
    from knowseam.graph.facts import FactRecorder
    from knowseam.graph.worker import GraphWorker

    setup(case)
    original, extractor, projection, cache, config = graph_setup(case)
    worker = GraphWorker(
        case.store,
        None,
        extractor,
        config.model_copy(update={"enabled": False}),
        case.company,
        extraction_cache=cache,
        fact_recorder=FactRecorder(case.store),
    )
    case.write("A costs B.")
    case.sync()
    assert run(worker.step())["status"] == "done"
    with case.store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM sk_fact_events WHERE company_id=%s", (case.company,)
            ).fetchone()["n"]
            == 1
        )


def test_many_supports_cannot_crowd_out_other_fact(case):
    from knowseam.history.domain import HistorySearchRequest

    history = setup(case)
    first = accept_fact(case, "B")
    accept_fact(case, "C")
    # 101 distinct authorized supporting chunks for one fact; other facts must remain candidates.
    with case.store.connect() as c:
        support = c.execute("SELECT * FROM sk_fact_supports WHERE observation_id=%s", (first,)).fetchone()
        c.execute(
            "INSERT INTO sk_chunks SELECT version_id,'support-'||g,100+g,text,heading,embedding_text,content_hash,locator,tokens,embedding,token_count "
            "FROM sk_chunks CROSS JOIN generate_series(1,101) g WHERE version_id=%s AND chunk_id=%s",
            (support["version_id"], support["chunk_id"]),
        )
        c.execute(
            "INSERT INTO sk_fact_supports SELECT %s,%s,'support-'||g,%s,%s,NULL FROM generate_series(1,101) g",
            (first, support["version_id"], support["quote"], support["recorded_from"]),
        )
    result = history.search_history(
        case.principal, HistorySearchRequest(query="A", valid_as_of="2021-01-01T00:00:00Z")
    )
    assert {r["object"] for r in result["observations"]} == {"B", "C"}
    assert all(len(r["citations"]) <= 3 for r in result["observations"])


def after_first_history_query(store, action):
    from contextlib import contextmanager

    original = store.connect
    done = False

    class Connection:
        def __init__(self, c):
            self.c, self.queried = c, False

        def execute(self, sql, args=None):
            self.queried |= sql.startswith("WITH eligible")
            return self.c.execute(sql, args)

        def __getattr__(self, name):
            return getattr(self.c, name)

    @contextmanager
    def connect():
        nonlocal done
        with original() as c:
            wrapper = Connection(c)
            yield wrapper
        if wrapper.queried and not done:
            done = True
            action()

    store.connect = connect


def test_correction_during_history_read_drops_closed_fact(case):
    from knowseam.history.domain import HistorySearchRequest
    from knowseam.graph.facts import FactRecorder

    history = setup(case)
    first = accept_fact(case, "B")
    second = accept_fact(case, "C", filename="other.md")
    after_first_history_query(
        case.store, lambda: FactRecorder(case.store).correct(case.company, first, second, event_id="race")
    )
    result = history.search_history(
        case.principal, HistorySearchRequest(query="A", valid_as_of="2021-01-01T00:00:00Z")
    )
    assert {o["object"] for o in result["observations"]} == {"C"}


def test_revoked_support_does_not_leak_earlier_acceptance_time(case):
    from knowseam.history.domain import HistorySearchRequest

    history = setup(case)
    first = accept_fact(case, "B")
    accept_fact(case, "B", filename="other.md")
    hidden = case.document()["id"]
    visible = case.document("other.md")["active_version"]
    with case.store.connect() as c:
        expected = (
            c.execute(
                "SELECT recorded_from FROM sk_fact_supports WHERE observation_id=%s AND version_id=%s",
                (first, visible),
            )
            .fetchone()["recorded_from"]
            .isoformat()
        )

    def revoke():
        with case.store.connect() as c:
            c.execute("UPDATE sk_documents SET public=false WHERE id=%s", (hidden,))

    after_first_history_query(case.store, revoke)
    result = history.search_history(
        case.principal, HistorySearchRequest(query="A", valid_as_of="2021-01-01T00:00:00Z")
    )
    assert len(result["observations"]) == 1
    assert result["observations"][0]["recorded_from"] == expected
    assert len(result["observations"][0]["citations"]) == 1
