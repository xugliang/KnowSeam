from test_postgres_e2e import case as _case, checked_dsn as _checked_dsn, tokenizer as _tokenizer

from test_temporal_postgres_e2e import setup, accept_fact

case, checked_dsn, tokenizer = _case, _checked_dsn, _tokenizer


def test_authorized_facts_filtered_before_budget_and_current_versions(case):
    from knowseam.graph.query import PostgresFactCandidates

    setup(case)
    accept_fact(case, "B")
    accept_fact(case, "C", filename="hidden.md")
    provider = PostgresFactCandidates(case.store)
    with case.store.connect() as c:
        c.execute("UPDATE sk_documents SET public=false WHERE id=%s", (case.document("hidden.md")["id"],))
    facts = provider.authorized_fact_candidates(case.principal, case.collection, "A price", (), 1)
    assert len(facts) == 1 and facts[0].quote == "A costs B since 2020-01-01T00:00:00Z."
    assert provider.authorized_fact_candidates(case.principal, case.collection, "Unknown price", (), 10) == []
    case.write("Unrelated new text")
    case.sync()
    # Sync legitimately refreshes the public filesystem ACL of hidden.md too.
    assert all(
        e.evidence.document_id != case.document()["id"]
        for e in provider.authorized_fact_candidates(case.principal, case.collection, "A price", (), 10)
    )
