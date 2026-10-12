import pytest
from test_postgres_e2e import case as _case, checked_dsn as _checked_dsn, tokenizer as _tokenizer, run

from test_visual_assets import png
from knowseam.domain import SourceContent, SourceNode, SourceSnapshot, NotFound
from knowseam.migrations.runner import MigrationRunner

case, checked_dsn, tokenizer = _case, _checked_dsn, _tokenizer


def visual_setup(case):
    from knowseam.visual.domain import VisualConfig, MultiVector
    from knowseam.visual.postgres import PostgresVisual
    from knowseam.visual.assets import VisualParser
    from knowseam.visual.worker import VisualWorker

    MigrationRunner(case.store.connect).apply()
    config = VisualConfig(
        model_revision="pinned",
        model_sha256="a" * 64,
        processor_sha256="b" * 64,
        dependency_sha256="c" * 64,
        dimensions=2,
    )
    backend = PostgresVisual(case.store, config, case.worker.file_root)
    case.store.visual_jobs = backend
    case.worker.parser = VisualParser(case.worker.parser)

    class Encoder:
        fingerprint = config.fingerprint

        async def ready(self):
            return True

        async def encode_assets(self, assets):
            return [MultiVector(dimensions=2, vectors=[[1.0, 0.0]]) for _ in assets]

        async def encode_queries(self, texts):
            return [MultiVector(dimensions=2, vectors=[[1.0, 0.0]]) for _ in texts]

    return backend, VisualWorker(backend, Encoder(), case.company), Encoder()


class ImageSource:
    data = png()
    revision = "1"

    async def scan(self):
        return SourceSnapshot((SourceNode("image", "Image", self.revision, "image/png", "", True),))

    async def fetch(self, node):
        return SourceContent(self.data, "image.png", "image/png")


def test_visual_only_publication_index_read_and_new_version_rejects_old(case):
    from knowseam.visual.service import VisualService
    from knowseam.visual.domain import VisualSearchRequest

    backend, worker, encoder = visual_setup(case)
    source = ImageSource()
    result = run(case.worker.run(case.source, source))
    assert result["published"] == 1 and result["failed"] == 0
    assert run(worker.step())["status"] == "done"
    service = VisualService(backend, encoder)
    results = run(service.search(case.principal, VisualSearchRequest(query="figure")))
    assert len(results["hits"]) == 1 and results["hits"][0]["modality"] == "visual"
    hit = results["hits"][0]
    content = backend.read_asset(case.principal, hit["asset_id"], hit["version_id"])
    assert content.startswith(b"\x89PNG")
    with case.store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM sk_chunks WHERE version_id=%s", (hit["version_id"],)
            ).fetchone()["n"]
            == 0
        )
    source.data, source.revision = png(color="blue"), "2"
    assert run(case.worker.run(case.source, source))["published"] == 1
    with pytest.raises(NotFound):
        backend.read_asset(case.principal, hit["asset_id"], hit["version_id"])


def test_visual_job_stale_lease_and_revocation_cannot_publish(case):
    from knowseam.visual.domain import MultiVector
    from knowseam.visual.assets import render_assets

    backend, worker, encoder = visual_setup(case)
    run(case.worker.run(case.source, ImageSource()))
    job = backend.claim(case.company)
    assets = render_assets(ImageSource.data, job["requests"])
    with case.store.connect() as c:
        c.execute("UPDATE sk_visual_jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job["id"],))
    assert not backend.commit(job, assets, [MultiVector(dimensions=2, vectors=[[1.0, 0.0]])])
    replacement = backend.claim(case.company)
    assert replacement["claim_token"] != job["claim_token"]
    with case.store.connect() as c:
        c.execute("UPDATE sk_documents SET public=false WHERE id=%s", (replacement["document_id"],))
    assert not backend.commit(replacement, assets, [MultiVector(dimensions=2, vectors=[[1.0, 0.0]])])
    with case.store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM sk_visual_assets WHERE version_id=%s", (job["version_id"],)
            ).fetchone()["n"]
            == 0
        )


def test_visual_transient_obsolete_recovers_and_failed_jobs_need_explicit_rebuild(case):
    backend, worker, _ = visual_setup(case)
    run(case.worker.run(case.source, ImageSource()))
    job = backend.claim(case.company)
    backend.finish(job)  # the same generation became temporarily unavailable
    backend.ensure(case.company)
    retry = backend.claim(case.company)
    assert retry is not None and retry["claim_token"] != job["claim_token"]
    assert retry["attempts"] == 2
    with case.store.connect() as c:
        c.execute("UPDATE sk_visual_jobs SET state='failed',attempts=3 WHERE id=%s", (retry["id"],))
    backend.ensure(case.company)
    assert backend.claim(case.company) is None
    backend.ensure(case.company, retry_failed=True)
    retry = backend.claim(case.company)
    assert retry is not None and retry["attempts"] == 1
    assert run(worker.step())["status"] == "idle"  # the active claim is not duplicated


def test_visual_reconciliation_is_bounded_and_has_a_continuation(case):
    backend, _, _ = visual_setup(case)
    run(case.worker.run(case.source, ImageSource()))
    first = backend.ensure(case.company, limit=1)
    assert first["checked"] == 1 and first["next_cursor"]
    second = backend.ensure(case.company, limit=1, after=first["next_cursor"])
    assert second == {"checked": 0, "next_cursor": None}
