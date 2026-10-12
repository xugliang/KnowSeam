import pytest

from test_postgres_e2e import case as _case, checked_dsn as _checked_dsn, tokenizer as _tokenizer

from knowseam.catalog.domain import CatalogConfig, CatalogRequest
from knowseam.catalog.postgres import CatalogPublisher, PostgresCatalogReader, configure_catalog
from knowseam.catalog.service import CursorStale
from knowseam.migrations.runner import MigrationRunner

case, checked_dsn, tokenizer = _case, _checked_dsn, _tokenizer


def setup_catalog(case):
    MigrationRunner(case.store.connect).apply()
    config = CatalogConfig.model_validate(
        {
            "entries": [
                {
                    "source_id": case.source,
                    "external_id": f"d{i}.md",
                    "kind": "product",
                    "item_id": f"item{i}",
                    "name": f"示例{i}",
                }
                for i in range(6)
            ]
        }
    )
    configure_catalog(case.store, case.company, config)
    case.worker.structure_publisher = CatalogPublisher(case.store, config)
    for i in range(6):
        case.write(f"示例产品{i}支持资料导出。", name=f"d{i}.md")
    assert case.sync()["published"] == 7  # six declarations plus the fixture's unregistered guide
    return PostgresCatalogReader(case.store), CatalogRequest(
        collection_ids=[case.collection], kind="product", limit=2
    )


def test_catalog_full_enumeration_not_top_k_and_revocation(case):
    reader, request = setup_catalog(case)
    page = reader.list_catalog(case.principal, request)
    assert page["catalog_status"] == "complete"
    items = page["items"][:]
    while page["next_cursor"]:
        page = reader.list_catalog(case.principal, request.model_copy(update={"cursor": page["next_cursor"]}))
        items += page["items"]
    assert [i["item_id"] for i in items] == [f"item{i}" for i in range(6)]
    assert all(i["evidence"][0]["version_id"] for i in items)
    first = reader.list_catalog(case.principal, request)
    case.store.fail_document(case.document("d5.md")["id"], "revoked", revoke=True)
    with pytest.raises(CursorStale):
        reader.list_catalog(case.principal, request.model_copy(update={"cursor": first["next_cursor"]}))
    current = reader.list_catalog(case.principal, request.model_copy(update={"limit": 100}))
    assert "item5" not in {i["item_id"] for i in current["items"]}


def test_catalog_hidden_changes_do_not_invalidate_visible_cursor(case):
    reader, request = setup_catalog(case)
    hidden = case.document("d5.md")["id"]
    case.store.fail_document(hidden, "revoked", revoke=True)
    first = reader.list_catalog(case.principal, request)
    with case.store.connect() as c:
        c.execute(
            "UPDATE sk_catalog_items SET name='hidden-changed' WHERE collection_id=%s AND item_id='item5'",
            (case.collection,),
        )
    page = reader.list_catalog(case.principal, request.model_copy(update={"cursor": first["next_cursor"]}))
    assert page["items"]
    assert "hidden-changed" not in str(page)


def test_catalog_partial_scan_and_unregistered_kind(case):
    reader, request = setup_catalog(case)
    case.connector.complete = False
    case.sync()
    assert reader.list_catalog(case.principal, request)["catalog_status"] == "partial"
    missing = reader.list_catalog(case.principal, request.model_copy(update={"kind": "term-dictionary"}))
    assert missing["catalog_status"] == "unavailable"
    assert missing["items"] == []
