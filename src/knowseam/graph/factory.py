"""Factories keep optional drivers and network clients outside graph-disabled deployments."""

from .config import GraphConfig
from .domain import GraphExtractor, GraphStore


def build_graph_store(config: GraphConfig) -> GraphStore | None:
    if not config.enabled:
        return None
    from .neo4j_store import Neo4jGraphStore
    return Neo4jGraphStore(config)


def build_graph_extractor(config: GraphConfig, *, for_facts=False) -> GraphExtractor | None:
    if not config.enabled and not for_facts:
        return None
    if config.extractor is None:
        raise ValueError('fact_extractor_configuration_required')
    from .extraction import OpenAIChatExtractor
    return OpenAIChatExtractor(config.extractor)
