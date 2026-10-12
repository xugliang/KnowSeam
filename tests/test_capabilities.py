"""Optional features cannot silently become requirements of existing adapters."""
import asyncio
import importlib.util
import sys

import pytest

from knowseam.domain import Principal
from knowseam.repository import KnowledgeRepository


def module():
    assert importlib.util.find_spec("knowseam.capabilities") is not None, "optional capability seam missing"
    from knowseam import capabilities
    return capabilities


def test_legacy_adapter_needs_no_new_abstract_method():
    cap = module()
    assert not ({"list_catalog", "read_context", "read_table", "search_history"}
                & KnowledgeRepository.__abstractmethods__)
    registry = cap.CapabilitySet()
    assert registry.available(Principal("a", "c", "p", ("one",))) == {
        "catalog": False, "context": False, "tables": False,
        "history": False, "visual": False, "query_graph": False,
    }
    with pytest.raises(cap.CapabilityUnavailable):
        registry.require("context")


def test_capability_disabled_no_optional_import():
    cap = module()
    before = set(sys.modules)
    registry = cap.CapabilitySet()
    registry.available(Principal("a", "c", "p", ("one",)))
    assert not any(name.startswith(("torch", "transformers", "neo4j")) for name in set(sys.modules) - before)


def test_capability_requires_current_authorized_collection():
    cap = module()
    from knowseam.service import KnowledgeService

    class Store:
        def collections(self, principal):
            return []

    service = KnowledgeService(Store(), None, None, None, capabilities=cap.CapabilitySet(context=object()))
    result = asyncio.run(service.get_capabilities(Principal("a", "c", "p", ("one",))))
    assert result["context"] is False
