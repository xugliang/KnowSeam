import pytest

from test_postgres_e2e import case as _case, checked_dsn as _checked_dsn, tokenizer as _tokenizer

from knowseam.migrations.runner import MigrationRunner

case, checked_dsn, tokenizer = _case, _checked_dsn, _tokenizer


def test_reuse_publication_counts_ranking_and_explicit_legacy_rollback(case):
    from knowseam.indexing.postgres import PostgresTokenCache, materialize_legacy

    MigrationRunner(case.store.connect).apply()
    case.store.indexing_enabled = True
    case.worker.indexing = PostgresTokenCache(case.store, mode="reuse")
    case.write("重复内容 示例产品\n\n重复内容 示例产品\n\n不同内容 示例产品")
    assert case.sync()["published"] == 1
    doc = case.document()
    with case.store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM sk_term_postings WHERE version_id=%s", (doc["active_version"],)
            ).fetchone()["n"]
            == 3
        )
        assert (
            c.execute(
                "SELECT count(DISTINCT payload_id) n FROM sk_term_postings WHERE version_id=%s",
                (doc["active_version"],),
            ).fetchone()["n"]
            == 2
        )
        assert (
            c.execute(
                "SELECT count(*) n FROM sk_chunk_terms WHERE version_id=%s", (doc["active_version"],)
            ).fetchone()["n"]
            == 0
        )
    query = dict(vector=[1, 0.2, 0.1], model_id=case.embedder.model_id, dimensions=3, tokens=["示例产品"])
    lexical = case.store.retrieve(case.principal, None, **query)[1]
    assert len(lexical) == 3
    case.store.indexing_enabled = False
    assert not case.store.ready()
    with pytest.raises(RuntimeError, match="rollback"):
        case.store.retrieve(case.principal, None, **query)
    assert materialize_legacy(case.store, case.company) == 3
    assert case.store.ready()
    legacy = case.store.retrieve(case.principal, None, **query)[1]
    assert [(h.chunk_id, h.score) for h in legacy] == [(h.chunk_id, h.score) for h in lexical]


def test_unchanged_payload_reused_after_insert_and_dictionary_invalidation(case):
    from knowseam.indexing.postgres import PostgresTokenCache
    from knowseam.tokenization import JiebaTokenizer

    MigrationRunner(case.store.connect).apply()
    case.worker.indexing = PostgresTokenCache(case.store, mode="shadow")
    case.store.indexing_enabled = True
    case.write("示例产品 第一段\n\n示例产品 第二段")
    assert case.sync()["published"] == 1
    case.write("新增段\n\n示例产品 第一段\n\n示例产品 第二段")
    assert case.sync()["published"] == 1
    assert case.worker.indexing.last_stats == {"cache_hits": 2, "tokenized_inputs": 1, "postings": 3}
    with case.store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM sk_token_cache WHERE company_id=%s", (case.company,)
            ).fetchone()["n"]
            == 3
        )
    case.worker.tokenizer = JiebaTokenizer(terms=("新增词汇",))
    assert case.sync()["published"] == 1
    assert case.worker.indexing.last_stats["cache_hits"] == 0
