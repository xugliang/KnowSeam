from test_postgres_e2e import case as _case, checked_dsn as _dsn, tokenizer as _tokenizer
from test_temporal_postgres_e2e import setup, accept_fact

case, checked_dsn, tokenizer = _case, _dsn, _tokenizer


def test_conflict_requires_two_currently_visible_supports(case):
    from knowseam.graph.domain import GraphOntology
    from knowseam.history.domain import HistorySearchRequest
    history = setup(case)
    history.ontologies = {case.collection: GraphOntology(
        entity_types=("p",), relation_types=("price",), single_valued_relations=("price",))}
    accept_fact(case, "B")
    accept_fact(case, "C", filename="other.md")
    request = HistorySearchRequest(query="A", valid_as_of="2021-01-01T00:00:00Z")
    visible = history.search_history(case.principal, request)
    assert len(visible["observations"]) == 2 and len(visible["conflicts"]) == 1
    case.store.fail_document(case.document("other.md")["id"], "revoked", revoke=True)
    current = history.search_history(case.principal, request)
    assert len(current["observations"]) == 1
    assert current["conflicts"] == []
    assert "C" not in {o["object"] for o in current["observations"]}
