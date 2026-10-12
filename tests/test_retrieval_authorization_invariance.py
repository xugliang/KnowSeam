from test_postgres_e2e import case as _case, checked_dsn as _checked_dsn, tokenizer as _tokenizer

from knowseam.migrations.runner import MigrationRunner

case, checked_dsn, tokenizer = _case, _checked_dsn, _tokenizer


def test_new_postings_lookup_index_and_hidden_documents_invariance(case):
    from knowseam.indexing.postgres import PostgresTokenCache, materialize_legacy

    MigrationRunner(case.store.connect).apply()
    case.store.indexing_enabled = True
    case.worker.indexing = PostgresTokenCache(case.store, mode="reuse")
    case.write("示例产品 第一段\n\n示例产品 第二段")
    case.sync()
    query = dict(vector=[1, 0.2, 0.1], model_id=case.embedder.model_id, dimensions=3, tokens=["示例产品"])

    def scores():
        return [
            (h.document_id, h.chunk_id, h.score)
            for h in case.store.retrieve(case.principal, None, **query)[1]
        ]

    baseline = scores()
    case.write("\n\n".join("示例产品 隐藏资料 " + str(i) for i in range(200)), "hidden.md")
    case.sync()
    with case.store.connect() as c:
        c.execute("UPDATE sk_documents SET public=false WHERE id=%s", (case.document("hidden.md")["id"],))
        assert c.execute("SELECT to_regclass('sk_term_postings_payload') name").fetchone()["name"] is not None
    assert scores() == baseline
    materialize_legacy(case.store, case.company)
    case.store.indexing_enabled = False
    assert scores() == baseline


def test_bm25_statistics_are_not_recomputed_per_candidate(case):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from benchmark_retrieval import capture_sql

    case.write("\n\n".join("alpha record " + str(i) for i in range(100)))
    case.sync()
    captured = {}
    with capture_sql(case.store, captured):
        case.store.retrieve(
            case.principal,
            None,
            vector=[1, 0.2, 0.1],
            model_id=case.embedder.model_id,
            dimensions=3,
            tokens=["alpha"],
        )
    sql, args = captured["lexical"]
    with case.store.connect() as c:
        plan = c.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + sql, args).fetchone()["QUERY PLAN"][0]["Plan"]

    def walk(node):
        yield node
        for child in node.get("Plans", ()):
            yield from walk(child)

    # Reading the eligible corpus once for N/avgdl is linear; doing so per hit is quadratic.
    scans = [n for n in walk(plan) if n.get("CTE Name") == "eligible"]
    assert scans and max(n["Actual Loops"] for n in scans) == 1
