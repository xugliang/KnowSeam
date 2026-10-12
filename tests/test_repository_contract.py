from abc import ABC

import pytest

from knowseam.domain import AccessDenied, Principal


def test_repository_owns_public_scope_selection_and_rejects_untrusted_scope():
    from knowseam.repository import KnowledgeRepository, RetrievalBackend
    from knowseam.storage import PostgresStore

    assert issubclass(KnowledgeRepository, ABC)
    assert issubclass(PostgresStore, KnowledgeRepository)
    assert issubclass(PostgresStore, RetrievalBackend)
    store = PostgresStore("postgresql://unused/knowseam_test")
    principal = Principal("employee", "company", "product", ("public",))
    assert store.select_collections(principal, None) == ["public"]
    with pytest.raises(AccessDenied):
        store.select_collections(principal, ["secret"])
    with pytest.raises(AccessDenied):
        store.select_collections(Principal("", "company", "product", ("public",)), None)
