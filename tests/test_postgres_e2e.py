"""Real PostgreSQL/pgvector integration, not model-quality evaluation.

Run only against an explicitly configured dedicated database named exactly
``knowseam_test``. Every case uses unique company/source/collection IDs;
no DROP/TRUNCATE or cleanup of another run's rows is performed. Source bytes are
real temporary files, parsing and Jieba are real, only external embedding and
reranking inference use deterministic substitutes. Fault-injection subclasses
wrap the real filesystem connector to exercise partial scans and revocation.
"""
# ruff: noqa: E402 -- skip safely before importing optional PostgreSQL dependencies.

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import inspect
import os
from pathlib import Path
import uuid

import pytest

TEST_DSN = os.environ.get("SUITE_KNOWLEDGE_TEST_DSN", "")
if not TEST_DSN:
    pytest.skip("real PostgreSQL integration requires SUITE_KNOWLEDGE_TEST_DSN (knowseam_test only)",
                allow_module_level=True)

import psycopg
from psycopg.conninfo import conninfo_to_dict

from knowseam.chunking import chunk_document, digest, parsed_hash
from knowseam.connectors.filesystem import FilesystemConnector
from knowseam.domain import AccessDenied, Embedder, NotFound, Principal, Reranker, SourceSnapshot
from knowseam.parsers import StructuredDocumentParser
from knowseam.service import KnowledgeService
from knowseam.storage import PostgresStore, document_id
from knowseam.sync import SyncWorker
from knowseam.tokenization import JiebaTokenizer


def run(awaitable):
    return asyncio.run(awaitable)


class FixedEmbedder(Embedder):
    """Vectors test persistence/control flow only; they are not semantic embeddings."""
    dimensions = 3

    def __init__(self, model_id):
        self.model_id = model_id
        self.calls = []
        self.fail = False

    async def embed(self, texts, *, query=False):
        self.calls.append((tuple(texts), query))
        if self.fail:
            raise RuntimeError("injected embedding outage")
        return [[1.0, 0.2, 0.1] for _ in texts]


class FixedReranker(Reranker):
    model_id = "test-only-reranker-not-quality-evaluation"

    def __init__(self):
        self.calls = []
        self.on_score = None

    async def score(self, query, documents):
        self.calls.append((query, tuple(documents)))
        if self.on_score:
            callback, self.on_score = self.on_score, None
            value = callback()
            if inspect.isawaitable(value):
                await value
        return [1.0 if query in document else 0.5 for document in documents]


class ControlledFiles(FilesystemConnector):
    def __init__(self, root):
        super().__init__(root, public_files=("guide.md",))
        self.public_overrides = {}
        self.omit = set()
        self.complete = True
        self.fetch_failure = None
        self.on_scan = None

    async def scan(self):
        snapshot = await super().scan()
        nodes = tuple(replace(node, public=self.public_overrides.get(node.external_id, node.public))
                      for node in snapshot.nodes if node.external_id not in self.omit)
        if self.on_scan:
            callback, self.on_scan = self.on_scan, None
            value = callback()
            if inspect.isawaitable(value):
                await value
        return SourceSnapshot(nodes, self.complete and snapshot.complete,
                              snapshot.errors + (() if self.complete else ("injected partial source enumeration",)))

    async def fetch(self, node):
        if self.fetch_failure:
            raise self.fetch_failure
        return await super().fetch(node)


@pytest.fixture(scope="session")
def checked_dsn():
    # Never execute even schema CREATE on an ambient, production, or unnamed DB.
    try:
        parameters = conninfo_to_dict(TEST_DSN)
    except Exception:
        pytest.fail("invalid SUITE_KNOWLEDGE_TEST_DSN; connection not attempted", pytrace=False)
    if parameters.get("dbname") != "knowseam_test":
        pytest.fail("refusing database other than knowseam_test; connection not attempted", pytrace=False)
    with psycopg.connect(TEST_DSN, connect_timeout=5) as connection:
        if connection.execute("SELECT current_database()").fetchone()[0] != "knowseam_test":
            pytest.fail("server database identity check failed before schema initialization", pytrace=False)
    store = PostgresStore(TEST_DSN)
    store.initialize()
    assert store.ready()
    return TEST_DSN


@pytest.fixture(scope="session")
def tokenizer():
    tokenizer = JiebaTokenizer(terms=("测试产品", "专属功能", "示例产品"))
    assert "示例产品" in tokenizer.tokenize("示例产品支持公司知识检索")
    return tokenizer


@dataclass
class Case:
    store: PostgresStore
    company: str
    collection: str
    source: str
    principal: Principal
    connector: ControlledFiles
    embedder: FixedEmbedder
    reranker: FixedReranker
    worker: SyncWorker
    service: KnowledgeService
    root: Path
    config_hash: str = "fixture-config-v1"

    def configure(self, *, enabled=True, config_hash="fixture-config-v1"):
        self.store.configure(company_id=self.company,
                             collections=[{"id": self.collection, "title": "集成测试公开资料"}],
                             sources=[{"id": self.source, "collection_id": self.collection,
                                       "config_hash": config_hash}] if enabled else [])
        self.config_hash = config_hash

    def sync(self, *, force=False):
        return run(self.worker.run(self.source, self.connector, force=force, config_hash=self.config_hash))

    def write(self, text, name="guide.md"):
        (self.root / name).write_text(text, encoding="utf-8")
        self.connector.public_files = self.connector.public_files | {name}

    def document(self, name="guide.md"):
        with self.store.connect() as connection:
            return connection.execute("SELECT * FROM sk_documents WHERE id=%s",
                                      (document_id(self.source, name),)).fetchone()

    def versions(self, name="guide.md"):
        with self.store.connect() as connection:
            return connection.execute("SELECT * FROM sk_versions WHERE document_id=%s ORDER BY created_at,id",
                                      (document_id(self.source, name),)).fetchall()

    def search(self, query="示例产品", principal=None):
        return run(self.service.search(principal or self.principal, query))

    def read(self, name="guide.md", *, principal=None, version=None, offset=0, limit=50):
        document = self.document(name)
        return run(self.service.read(principal or self.principal, document["id"],
                                     version or document["active_version"], offset=offset, limit=limit))


@pytest.fixture
def case(checked_dsn, tokenizer, tmp_path):
    suffix = uuid.uuid4().hex
    root = tmp_path / "published"
    root.mkdir()
    (root / "guide.md").write_text("# 示例产品产品线\n\n示例产品支持标书分析、产品功能查询。参考编号TEST-00000001。", encoding="utf-8")
    store = PostgresStore(checked_dsn, acl_ttl_seconds=900)
    company, collection, source = (f"test-company-{suffix}", f"test-collection-{suffix}", f"test-source-{suffix}")
    embedder, reranker = FixedEmbedder("fixed-integration-" + suffix), FixedReranker()
    parser = StructuredDocumentParser()
    value = Case(store, company, collection, source,
                 Principal("employee-a", company, "product-a", (collection,)), ControlledFiles(root),
                 embedder, reranker,
                 SyncWorker(store, parser, embedder, tokenizer, tmp_path / "archive"),
                 KnowledgeService(store, embedder, reranker, tokenizer), root)
    value.configure()
    try:
        yield value
    finally:
        # Imported session fixtures can run again in another module. Preserve
        # each case's records for debugging, but do not leave its sources live:
        # a reuse-mode case would correctly make a later legacy health check
        # fail. This changes only this random test company's configuration.
        store.configure(company_id=company, collections=[], sources=[])


def test_real_sync_retrieve_rerank_read_preserves_citations_and_pg_lexical(case):
    assert case.sync()["published"] == 1
    response = case.search("TEST-00000001")
    assert response["hits"] and case.reranker.calls
    hit = response["hits"][0]
    assert hit["document_id"] == document_id(case.source, "guide.md")
    assert hit["source_url"] == (case.root / "guide.md").as_uri()
    assert hit["locator"]["line_start"] in (1, 3)
    content = case.read()
    assert content["citations"] and content["citations"][0]["version_id"] == hit["version_id"]
    _, lexical = case.store.retrieve(case.principal, None, vector=[1, 0.2, 0.1], model_id=case.embedder.model_id,
                                    dimensions=3, tokens=case.worker.tokenizer.fts_text("TEST-00000001").split())
    assert any("TEST-00000001" in item.text for item in lexical)


def policy(case, visibility, subjects=(), employees=()):
    collection = {"id": case.collection, "title": "授权资料", "visibility": visibility}
    if visibility != "company":
        collection["departments" if visibility == "department" else "employees"] = subjects
    case.store.configure(company_id=case.company, collections=[collection], employees=employees,
                         sources=[{"id": case.source, "collection_id": case.collection,
                                   "config_hash": case.config_hash}])


@pytest.mark.parametrize("visibility", ["department", "employee"])
def test_restricted_acl_filters_both_channels_listing_read_and_revocation(case, visibility):
    case.sync()
    actor = case.principal.actor_id
    subjects = ["sales"] if visibility == "department" else [actor]
    employees = [{"actor_id": actor, "departments": ["sales"]}]
    policy(case, visibility, subjects, employees)
    assert case.search()["hits"]
    assert case.read()["sections"]
    outsider = replace(case.principal, actor_id="another-employee")
    assert case.store.collections(outsider) == []
    dense, lexical = case.store.retrieve(outsider, None, vector=[1, .2, .1], dimensions=3,
        model_id=case.embedder.model_id, tokens=["示例产品"])
    assert dense == lexical == []
    before = len(case.embedder.calls), len(case.reranker.calls)
    assert case.search(principal=outsider)["evidence_status"] == "no_accessible_collection"
    assert before == (len(case.embedder.calls), len(case.reranker.calls))
    with pytest.raises(NotFound):
        case.read(principal=outsider)
    case.reranker.on_score = lambda: policy(case, visibility, (), employees)
    assert not case.search()["hits"]
    assert not case.store.collections(case.principal)


def test_public_unchanged_but_disabled_employee_denied_and_no_cross_company(case):
    case.sync()
    assert case.search(principal=replace(case.principal, actor_id="new-employee"))["hits"]
    policy(case, "company", employees=[{"actor_id": case.principal.actor_id, "active": False}])
    assert case.store.collections(case.principal) == []
    assert case.store.collections(replace(case.principal, company_id="another-company")) == []
    with pytest.raises(NotFound):
        case.read()


def test_bm25_matches_reference_formula_and_migration_is_idempotent(case, tokenizer):
    import math
    from collections import Counter
    case.write("alpha alpha beta")
    case.write("alpha gamma gamma gamma gamma", "second.md")
    case.sync()
    with case.store.connect() as c:
        rows = c.execute("SELECT ch.* FROM sk_chunks ch JOIN sk_documents d ON d.active_version=ch.version_id "
                         "WHERE d.source_id=%s", (case.source,)).fetchall()
    assert len(rows) == 2
    counts = [Counter(tokenizer.tokenize(row["embedding_text"])) for row in rows]
    avgdl = sum(sum(c.values()) for c in counts) / len(counts)
    df = sum(c["alpha"] > 0 for c in counts)
    expected = {}
    for row, count in zip(rows, counts):
        tf = count["alpha"]
        expected[row["chunk_id"]] = math.log(1+(len(rows)-df+.5)/(df+.5))*tf*2.2/(tf+1.2*(.25+.75*sum(count.values())/avgdl))
    _, lexical = case.store.retrieve(case.principal, None, vector=[1, .2, .1], dimensions=3,
                                    model_id=case.embedder.model_id, tokens=["alpha", "alpha"])
    assert len(lexical) == 2
    for hit in lexical:
        assert hit.score == pytest.approx(expected[hit.chunk_id])
    before = len(case.embedder.calls)
    with case.store.connect() as c:
        c.execute("UPDATE sk_chunks SET token_count=NULL WHERE version_id=ANY(%s::text[])",
                  ([row["version_id"] for row in rows],))
    assert case.store.backfill_bm25(tokenizer, company_id=case.company, batch_size=1) == 2
    assert case.store.backfill_bm25(tokenizer, company_id=case.company) == 0
    assert len(case.embedder.calls) == before
    _, after = case.store.retrieve(case.principal, None, vector=[1, .2, .1], dimensions=3,
                                  model_id=case.embedder.model_id, tokens=["alpha"])
    assert [(x.chunk_id, x.score) for x in after] == [(x.chunk_id, x.score) for x in lexical]


def test_unchanged_sync_does_not_reembed_or_make_new_version(case):
    case.sync()
    before = len(case.embedder.calls)
    original = case.document()["active_version"]
    assert case.sync()["unchanged"] == 1
    assert len(case.embedder.calls) == before
    assert case.document()["active_version"] == original
    assert len(case.versions()) == 1


def test_modified_document_switches_version_and_old_version_cannot_be_read(case):
    case.sync()
    original = case.document()["active_version"]
    case.write("# 示例产品产品线\n\n示例产品新增专属功能：自动报价。")
    assert case.sync()["published"] == 1
    assert case.document()["active_version"] != original
    assert len(case.versions()) == 2
    with pytest.raises(NotFound):
        case.read(version=original)
    assert any("自动报价" in section["text"] for section in case.read()["sections"])


def test_embedding_failure_keeps_old_active_version_as_stale(case):
    case.sync()
    original = case.document()["active_version"]
    case.write("# 新产品\n\n这是全新内容。")
    case.embedder.fail = True
    assert case.sync()["failed"] == 1
    assert case.document()["active_version"] == original
    assert case.read()["stale"] is True
    assert len(case.versions()) == 1
    case.embedder.fail = False
    assert case.sync()["published"] == 1


def test_parser_failure_keeps_old_version_but_not_new_broken_content(case):
    case.sync()
    original = case.document()["active_version"]
    (case.root / "guide.md").write_bytes(b"\x00not text")
    assert case.sync()["failed"] == 1
    assert case.document()["active_version"] == original
    assert case.search()["hits"][0]["stale"] is True


def test_first_ingestion_failure_has_no_searchable_version(case):
    case.embedder.fail = True
    assert case.sync()["failed"] == 1
    assert case.document()["active_version"] is None
    case.embedder.fail = False
    assert case.search()["hits"] == []
    assert case.versions() == []


def test_partial_scan_keeps_missing_documents_and_revokes_observed_private(case):
    case.write("# Kino\n\nKino支持视频生成。", "other.md")
    case.sync()
    case.connector.omit = {"other.md"}
    case.connector.complete = False
    case.connector.public_overrides["guide.md"] = False
    result = case.sync()
    assert result["deleted"] == 0 and result["unavailable"] == 1
    assert case.document("other.md")["public"] is True
    assert case.document()["public"] is False
    hits = case.search()["hits"]
    assert hits and all(hit["document_id"] == document_id(case.source, "other.md") for hit in hits)


@pytest.mark.parametrize("publication", [False, None], ids=["revoked", "unknown"])
def test_publication_revoked_or_unknown_blocks_search_and_read_then_recovers(case, publication):
    case.sync()
    original = case.document()["active_version"]
    case.connector.public_overrides["guide.md"] = publication
    assert case.sync()["unavailable"] == 1
    assert case.search()["hits"] == []
    with pytest.raises(NotFound):
        case.read(version=original)
    case.connector.public_overrides.clear()
    assert case.sync()["unchanged"] == 1
    assert case.search()["hits"]


def test_complete_deletion_removes_access_and_restore_can_reuse_version(case):
    case.sync()
    original_text = (case.root / "guide.md").read_text(encoding="utf-8")
    original = case.document()["active_version"]
    (case.root / "guide.md").unlink()  # Only this test's unique temporary source file.
    assert case.sync()["deleted"] == 1
    assert case.document()["status"] == "deleted"
    assert case.search()["hits"] == []
    case.write(original_text)
    assert case.sync()["unchanged"] == 1
    assert case.document()["active_version"] == original
    assert case.read()["stale"] is False


def test_actor_collection_and_company_boundaries_are_enforced_before_model(case):
    case.sync()
    unauthorized = replace(case.principal, actor_id="employee-b", allowed_collections=())
    calls = len(case.embedder.calls)
    assert case.search(principal=unauthorized)["hits"] == []
    assert len(case.embedder.calls) == calls
    with pytest.raises(AccessDenied):
        run(case.service.search(unauthorized, "示例产品", [case.collection]))
    with pytest.raises(NotFound):
        case.read(principal=unauthorized)
    other_company = replace(case.principal, company_id=case.company + "-other")
    assert case.search(principal=other_company)["hits"] == []
    with pytest.raises(NotFound):
        case.read(principal=other_company)
    # The v1 data is company-public: another authenticated employee with the
    # same allowed collection should see it, not an invented per-actor silo.
    assert case.search(principal=replace(case.principal, actor_id="employee-c"))["hits"]
    with pytest.raises(AccessDenied):
        case.search(principal=replace(case.principal, actor_id=""))


def test_expired_acl_blocks_both_paths_until_real_rescan(case):
    case.sync()
    with case.store.connect() as connection:
        connection.execute("UPDATE sk_documents SET acl_verified_at=now()-interval '1 hour' WHERE id=%s",
                           (case.document()["id"],))
    assert case.search()["hits"] == []
    with pytest.raises(NotFound):
        case.read()
    assert case.sync()["unchanged"] == 1
    assert case.search()["hits"]


def test_removed_source_configuration_immediately_hides_existing_content(case):
    case.sync()
    case.configure(enabled=False)
    assert case.search()["hits"] == []
    with pytest.raises(NotFound):
        case.read()


@pytest.mark.parametrize("failure", ["fetch", "embedding"])
def test_source_root_configuration_change_cannot_serve_previous_root_on_failure(case, failure):
    case.sync()
    original_config = case.config_hash
    original_version = case.document()["active_version"]
    original_archive = case.worker.file_root / case.versions()[0]["raw_path"]
    replacement_root = case.root.parent / "replacement"
    replacement_root.mkdir()
    (replacement_root / "guide.md").write_text("# 新根资料\n\n新根专属材料，与旧来源正文不同。", encoding="utf-8")
    case.configure(config_hash="new-allowed-root")
    case.root, case.connector = replacement_root, ControlledFiles(replacement_root)
    assert case.document()["active_version"] is None
    assert case.document()["source_revision"] == ""
    assert len(case.versions()) == 1 and original_archive.is_file()
    assert case.search()["hits"] == []
    with pytest.raises(AccessDenied):
        run(case.worker.run(case.source, case.connector, config_hash=original_config))
    if failure == "fetch":
        case.connector.fetch_failure = RuntimeError("injected new-root fetch failure")
    else:
        case.embedder.fail = True
    assert case.sync()["failed"] == 1
    case.embedder.fail = False  # Query inference must work to test the real ACL/version SQL.
    assert case.document()["active_version"] is None
    assert case.search()["hits"] == []
    with pytest.raises(NotFound):
        case.read(version=original_version)
    assert len(case.versions()) == 1 and original_archive.is_file()
    case.connector.fetch_failure = None
    assert case.sync()["published"] == 1
    assert case.document()["active_version"] != original_version
    assert len(case.versions()) == 2
    assert any("新根专属材料" in section["text"] for section in case.read()["sections"])


def test_source_removed_while_scan_is_in_flight_cannot_be_reauthorized(case):
    case.sync()
    original = case.document()["active_version"]
    case.write("# 新资料\n\n不应从已经移除的来源发布。")
    case.connector.on_scan = lambda: case.configure(enabled=False)
    try:
        case.sync()
    except AccessDenied:
        pass  # A configuration fence may invalidate the in-flight worker.
    except RuntimeError as exc:
        # An equally safe strategy serializes configuration against sync. The
        # caller must retry the explicitly rejected change after sync releases.
        assert str(exc) == "source_configuration_busy_retry_after_sync"
        assert case.document()["active_version"] == original
        case.configure(enabled=False)
    assert case.search()["hits"] == []
    assert case.document()["public"] is False
    with pytest.raises(AccessDenied):
        case.sync()  # An obsolete worker cannot resurrect a removed source.


def test_revoke_during_reranking_is_revalidated_before_return(case):
    case.sync()
    case.reranker.on_score = lambda: case.store.fail_document(case.document()["id"], "revoked", revoke=True)
    assert case.search()["hits"] == []
    assert case.reranker.calls


def test_update_failure_during_reranking_refreshes_stale_flag(case):
    case.sync()
    original = case.document()["active_version"]
    case.reranker.on_score = lambda: case.store.fail_document(case.document()["id"], "update_failed")
    response = case.search()
    assert response["hits"]
    assert all(hit["stale"] is True and hit["version_id"] == original for hit in response["hits"])
    assert case.document()["public"] is True


def test_read_chunk_pagination_has_no_gaps_and_keeps_citations(case):
    case.write("# 长文档\n\n" + "\n\n".join(f"第{i}段介绍示例产品功能。" for i in range(1, 12)))
    case.sync()
    offset, ordinals, chunk_ids = 0, [], []
    while offset is not None:
        page = case.read(offset=offset, limit=3)
        assert len(page["sections"]) <= 3
        assert len(page["citations"]) == len(page["sections"])
        ordinals.extend(item["ordinal"] for item in page["sections"])
        chunk_ids.extend(item["chunk_id"] for item in page["sections"])
        offset = page["next_offset"]
    assert ordinals == list(range(len(ordinals)))
    assert len(set(chunk_ids)) == len(chunk_ids)
    assert len(ordinals) == 12


def test_publish_sql_constraint_failure_rolls_back_whole_new_version(case):
    case.sync()
    old = case.document()["active_version"]
    node = run(case.connector.scan()).nodes[0]
    parsed = case.worker.parser.parse(run(case.connector.fetch(node)))
    chunk = chunk_document(parsed)[0]
    with pytest.raises(psycopg.errors.UniqueViolation):
        case.store.publish(doc_id=case.document()["id"], node=node, chunks=[chunk, chunk],
                           embeddings=[[1, 0, 0], [1, 0, 0]], token_texts=["示例产品", "示例产品"],
                           model_id=case.embedder.model_id, dimensions=3, pipeline_id=case.worker.pipeline_id,
                           raw_hash=digest(b"fixture"), content_hash=parsed_hash(parsed), raw_path="test-only",
                           warnings=[])
    assert case.document()["active_version"] == old
    assert len(case.versions()) == 1
    assert case.search()["hits"]


def test_different_embedding_model_cannot_retrieve_incompatible_old_vectors(case):
    case.sync()
    case.embedder.model_id += "-new-model"
    assert case.search()["hits"] == []
    assert case.sync()["published"] == 1
    assert case.search()["hits"]
    assert len(case.versions()) == 2


def test_fetch_access_denial_overrides_scan_publication_and_revokes_cached_version(case):
    case.sync()
    case.write("# 改动\n\n扫描后文档撤权。")
    case.connector.fetch_failure = AccessDenied("injected permission revoked after scan")
    assert case.sync()["failed"] == 1
    assert case.document()["public"] is False
    assert case.document()["acl_verified_at"] is None
    assert case.search()["hits"] == []
