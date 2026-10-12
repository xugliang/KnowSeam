"""Explicit optional capabilities; importing these never imports optional runtimes."""
from dataclasses import dataclass, fields
from typing import Any

from .domain import Principal


class CapabilityUnavailable(ValueError):
    """A configured capability is unavailable, not an empty successful result."""


@dataclass(frozen=True)
class CapabilitySet:
    catalog: Any = None
    context: Any = None
    tables: Any = None
    history: Any = None
    visual: Any = None
    query_graph: Any = None

    def available(self, principal: Principal) -> dict[str, bool]:
        authenticated = bool(principal.actor_id and principal.company_id
                             and principal.product_id and principal.allowed_collections)
        return {item.name: authenticated and getattr(self, item.name) is not None for item in fields(self)}

    def require(self, name: str):
        if name not in {item.name for item in fields(self)} or getattr(self, name) is None:
            raise CapabilityUnavailable("capability_unavailable")
        return getattr(self, name)


def build_capabilities(store, features) -> CapabilitySet:
    context, catalog, tables, history, query_graph = None, None, None, None, None
    visual = None
    if features.visual:
        with store.connect() as c:
            if c.execute("SELECT to_regclass('sk_visual_assets') AS name").fetchone()['name'] is None:
                raise CapabilityUnavailable('visual_migration_required')
        visual = getattr(store,'visual_service',None)
        if visual is None:
            raise CapabilityUnavailable('visual_runtime_not_configured')
    if features.context:
        with store.connect() as c:
            if c.execute("SELECT to_regclass('sk_sections') AS name").fetchone()['name'] is None:
                raise CapabilityUnavailable('structure_migration_required')
        from .context_store import PostgresContextReader
        context = PostgresContextReader(store)
    if features.catalog:
        with store.connect() as c:
            if c.execute("SELECT to_regclass('sk_catalog_items') AS name").fetchone()['name'] is None:
                raise CapabilityUnavailable('catalog_migration_required')
        from .catalog.postgres import PostgresCatalogReader
        catalog = PostgresCatalogReader(store)
    if features.tables:
        with store.connect() as c:
            if c.execute("SELECT to_regclass('sk_table_cells') AS name").fetchone()['name'] is None:
                raise CapabilityUnavailable('table_migration_required')
        from .tables.postgres import PostgresTableReader
        tables = PostgresTableReader(store)
    if features.history:
        with store.connect() as c:
            if c.execute("SELECT to_regclass('sk_fact_observations') AS name").fetchone()['name'] is None:
                raise CapabilityUnavailable('history_migration_required')
        from .history.postgres import PostgresHistory
        history = PostgresHistory(store, getattr(store,'fact_ontologies',{}))
    if features.query_graph:
        with store.connect() as c:
            if c.execute("SELECT to_regclass('sk_fact_supports') AS name").fetchone()['name'] is None:
                raise CapabilityUnavailable('fact_migration_required')
        from .graph.query import QueryAwareSearch, AuthorizedFactGraph
        config = getattr(store,'fact_graph_config',None)
        if config is None:
            raise CapabilityUnavailable('query_graph_configuration_required')
        query_graph = QueryAwareSearch(store,getattr(store,'query_projection',None) or AuthorizedFactGraph(),config)
    return CapabilitySet(context=context, catalog=catalog, tables=tables, history=history,query_graph=query_graph,visual=visual)
