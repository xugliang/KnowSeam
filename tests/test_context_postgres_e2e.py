from dataclasses import replace

import pytest

from test_postgres_e2e import case as _case, checked_dsn as _checked_dsn, tokenizer as _tokenizer

from knowseam.chunking import chunk_document, digest
from knowseam.context_store import PostgresContextReader
from knowseam.contracts import ContextRequest
from knowseam.domain import ChunkSpan, NotFound, ParsedDocument, Section, SourceNode
from knowseam.migrations.runner import MigrationRunner

case, checked_dsn, tokenizer = _case, _checked_dsn, _tokenizer


def setup_structure(case):
    MigrationRunner(case.store.connect).apply()
    case.worker.structure_publisher = case.store
    case.write("# 示例产品\n\n第一节有明确证据。\n\n第二节是原始内容。")
    assert case.sync()["published"] == 1
    doc = case.document()
    with case.store.connect() as c:
        chunks = c.execute(
            "SELECT * FROM sk_chunks WHERE version_id=%s ORDER BY ordinal", (doc["active_version"],)
        ).fetchall()
    return doc, chunks


def test_context_current_version_original_section_and_revocation(case, monkeypatch):
    doc, chunks = setup_structure(case)
    request = ContextRequest(
        document_id=doc["id"],
        version_id=doc["active_version"],
        chunk_ids=[chunks[0]["chunk_id"]],
        mode="section",
    )
    reader = PostgresContextReader(case.store)
    result = reader.read_context(case.principal, request)
    assert result["context_status"] == "available"
    assert result["ranges"][0]["evidence_kind"] == "original_section"
    import knowseam.context_store as context_store

    original = context_store.assemble_context

    def revoke(*args):
        value = original(*args)
        case.store.fail_document(doc["id"], "test_revoked", revoke=True)
        return value

    monkeypatch.setattr(context_store, "assemble_context", revoke)
    with pytest.raises(NotFound):
        reader.read_context(case.principal, request)


def test_structure_publication_failure_does_not_switch_active_version(case):
    doc, _ = setup_structure(case)
    parsed = ParsedDocument((Section("invalid span rollback"),))
    chunks = chunk_document(parsed, source_spans=True)
    bad = replace(chunks[0], spans=(ChunkSpan("s-0", 0, 999),))
    node = SourceNode("guide.md", "title", "new", "text/markdown", "https://example.test", True)
    before = len(case.versions())
    with pytest.raises(ValueError, match="span"):
        case.store.publish_structured(
            parsed,
            doc_id=doc["id"],
            node=node,
            chunks=[bad],
            embeddings=[[1.0, 0.2, 0.1]],
            token_texts=["invalid"],
            model_id=case.embedder.model_id,
            dimensions=3,
            pipeline_id="test",
            raw_hash=digest("x"),
            content_hash=digest("x"),
            raw_path="test",
            warnings=(),
        )
    assert case.document()["active_version"] == doc["active_version"]
    assert len(case.versions()) == before


def test_legacy_context_cannot_return_parent_and_wrong_version_denied(case):
    MigrationRunner(case.store.connect).apply()
    case.write("示例产品 old data")
    case.sync()
    doc = case.document()
    with case.store.connect() as c:
        chunk = c.execute(
            "SELECT chunk_id FROM sk_chunks WHERE version_id=%s LIMIT 1", (doc["active_version"],)
        ).fetchone()
    reader = PostgresContextReader(case.store)
    request = ContextRequest(
        document_id=doc["id"], version_id=doc["active_version"], chunk_ids=[chunk["chunk_id"]]
    )
    assert reader.read_context(case.principal, request)["context_status"] == "legacy_neighbors"
    with pytest.raises(ValueError, match="structure_unavailable"):
        reader.read_context(case.principal, request.model_copy(update={"mode": "section"}))
    case.write("示例产品 changed")
    case.sync(force=True)
    with pytest.raises(NotFound):
        reader.read_context(case.principal, request)
