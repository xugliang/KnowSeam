"""All optional capabilities share one publication and the same current authorization.

Real PostgreSQL, filesystem parsing and image rendering; deterministic model outputs.
This is protocol/correctness evidence, not model quality evaluation.
"""
from decimal import Decimal

import pytest

from test_postgres_e2e import case as _case, checked_dsn as _dsn, tokenizer as _tokenizer, run
from test_visual_resource_e2e import visual_setup
from test_visual_assets import png
from test_typed_tables import workbook_bytes
from test_temporal_postgres_e2e import accept_fact

case, checked_dsn, tokenizer = _case, _dsn, _tokenizer


def test_three_phase_publication_read_update_revoke_restore(case):
    from knowseam.capabilities import build_capabilities
    from knowseam.catalog.domain import CatalogConfig
    from knowseam.catalog.postgres import configure_catalog
    from knowseam.config import FeaturesConfig
    from knowseam.domain import NotFound
    from knowseam.graph.config import GraphConfig
    from knowseam.indexing.postgres import PostgresTokenCache
    from knowseam.parsers import StructuredDocumentParser
    from knowseam.publication import build_publisher
    from knowseam.visual.assets import VisualParser
    from knowseam.visual.service import VisualService
    from knowseam.visual.domain import VisualSearchRequest

    backend, visual_worker, encoder = visual_setup(case)
    case.store.visual_service = VisualService(backend, encoder)
    case.store.history_enabled = case.store.indexing_enabled = True
    case.store.fact_graph_config = GraphConfig(collections={
        case.collection: {"entity_types": ["p"], "relation_types": ["price"],
                          "single_valued_relations": ["price"]}})
    case.store.fact_ontologies = case.store.fact_graph_config.collections
    catalog = CatalogConfig(entries=[dict(source_id=case.source, external_id="guide.md",
        kind="product", item_id="example", name="示例产品")])
    configure_catalog(case.store, case.company, catalog)
    features = FeaturesConfig(context=True, catalog=True, tables=True, history=True,
                              query_graph=True, visual=True)
    case.worker.structure_publisher = build_publisher(case.store, features, catalog)
    case.worker.parser = VisualParser(StructuredDocumentParser(structured_tables=True))
    case.worker.indexing = PostgresTokenCache(case.store, mode="reuse")
    case.service.capabilities = build_capabilities(case.store, features)
    case.service.coverage_enabled = True
    (case.root / "prices.xlsx").write_bytes(workbook_bytes())
    (case.root / "figure.png").write_bytes(png())
    case.connector.public_files |= {"prices.xlsx", "figure.png"}
    accept_fact(case, "B")
    assert run(visual_worker.step())["status"] == "done"
    capabilities = run(case.service.get_capabilities(case.principal))
    assert all(capabilities.values())
    catalog_request = {"kind": "product", "collection_ids": [case.collection]}
    page = run(case.service.catalog(case.principal, catalog_request))
    assert [i["name"] for i in page["items"]] == ["示例产品"]
    guide = case.document()
    old = guide["active_version"]
    results = run(case.service.search(case.principal, "A", limit=20))
    hit = next(h for h in results["hits"] if h["document_id"] == guide["id"])
    window = run(case.service.context(case.principal, dict(document_id=guide["id"],
        version_id=old, chunk_ids=[hit["chunk_id"]], mode="section")))
    assert window["ranges"] and "A costs B" in str(window)
    prices = case.document("prices.xlsx")
    tables = run(case.service.table_list(case.principal, dict(document_id=prices["id"],
        version_id=prices["active_version"])))
    table_request = dict(document_id=prices["id"], version_id=prices["active_version"],
        table_id=tables["tables"][0]["table_id"])
    cells = run(case.service.table_read(case.principal, table_request))["cells"]
    assert Decimal(next(c["value"] for c in cells if c["row"] == 2 and c["column"] == 2)) == Decimal("-1234567890123456789.00100")
    assert next(c["value"] for c in cells if c["row"] == 2 and c["column"] == 1) == "0012"
    historical = dict(document_id=guide["id"], version_id=old)
    timeline = dict(query="A", valid_as_of="2021-01-01T00:00:00Z")
    assert run(case.service.history_search(case.principal, timeline))["observations"]
    graph = run(case.service.search(case.principal, "A price",
        options={"strategy": "relationship", "include_paths": True}))
    assert graph["graph_paths"]
    visual = run(case.service.visual_search(case.principal, VisualSearchRequest(query="figure")))
    asset = visual["hits"][0]
    assert run(case.service.read_asset(case.principal, asset["asset_id"], asset["version_id"])).startswith(b"\x89PNG")

    case.write("A costs B since 2020-01-01T00:00:00Z.\n\n新增说明。")
    assert case.sync()["published"] == 1
    assert case.worker.indexing.last_stats["cache_hits"] >= 1
    assert case.document()["active_version"] != old
    with pytest.raises(NotFound):
        case.read(version=old)
    assert "A costs B" in str(run(case.service.history_read(case.principal, historical)))
    run(visual_worker.step())  # unchanged image stays reusable

    # Revoke at the authoritative source boundary, not only hide it from the UI.
    for name in ("guide.md", "prices.xlsx", "figure.png"):
        case.connector.public_overrides[name] = False
    case.sync()
    assert case.search("A")["hits"] == []
    assert run(case.service.catalog(case.principal, catalog_request))["items"] == []
    assert run(case.service.history_search(case.principal, timeline))["observations"] == []
    assert run(case.service.search(case.principal, "A price", options={"strategy": "relationship"}))["hits"] == []
    assert run(case.service.visual_search(case.principal, VisualSearchRequest(query="figure")))["hits"] == []
    for operation in (
        lambda: case.service.history_read(case.principal, historical),
        lambda: case.service.table_read(case.principal, table_request),
        lambda: case.service.read_asset(case.principal, asset["asset_id"], asset["version_id"]),
    ):
        with pytest.raises(NotFound):
            run(operation())

    case.connector.public_overrides.clear()
    case.sync()
    assert case.search("A")["hits"] and case.read()["sections"]
    assert run(case.service.table_read(case.principal, table_request))["cells"]
    backend.ensure(case.company)
    for _ in range(4):
        run(visual_worker.step())
    restored = run(case.service.visual_search(case.principal, VisualSearchRequest(query="figure")))
    assert restored["hits"]
