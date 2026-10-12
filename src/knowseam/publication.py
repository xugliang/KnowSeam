"""One optional publication transaction, independently selected capabilities."""
from .contracts import StructurePublisher
from .chunking import canonical, digest
from .capabilities import build_capabilities


class ConfiguredPublisher(StructurePublisher):
    def __init__(self, store, features, catalog=None):
        self.store, self.features = store, features
        self.catalog = None
        if features.catalog:
            from .catalog.postgres import CatalogPublisher
            from .catalog.domain import CatalogConfig
            self.catalog = CatalogPublisher(store, catalog or CatalogConfig())
        self.identity = digest(canonical({'context': features.context, 'tables': features.tables,
            'catalog': self.catalog.identity if self.catalog else None, 'history':features.history or features.query_graph,
            'visual':store.visual_jobs.config.fingerprint if features.visual else None,'structure_version': 2}))

    def publish_structured(self, parsed, **publication):
        target = self.catalog or self.store
        return target.publish_structured(parsed, **publication,
                                         _write_context=self.features.context, _write_tables=self.features.tables)


def build_publisher(store, features, catalog=None):
    if not (features.context or features.tables or features.catalog or features.history or features.query_graph or features.visual):
        return None
    build_capabilities(store, features)  # Schema readiness only; never applies a migration.
    return ConfiguredPublisher(store, features, catalog)
