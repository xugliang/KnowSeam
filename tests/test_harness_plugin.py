"""Real SuiteHarness registry/executor contracts; no LLM or external network.

Set SUITEHARNESS_SOURCE_PATH to the directory containing ``suiteharness`` when
testing a source checkout; no private workspace path is assumed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from functools import wraps

import httpx
import pytest
from pydantic import BaseModel

_harness_source = os.environ.get("SUITEHARNESS_SOURCE_PATH")
if _harness_source:
    sys.path.insert(0, _harness_source)

pytest.importorskip("suiteharness", reason="optional plugin tests require the real SuiteHarness source/package")

from suiteharness.execution.in_memory import (  # noqa: E402
    InMemoryApprovalStore, InMemoryAuditJournal, InMemoryCapabilityAuthority, InMemoryToolRegistry,
)
from suiteharness.execution.models import (  # noqa: E402
    RunRequest, ToolCallStatus, ToolEffect, ToolIdentity, ToolIntent, WorkflowDecision,
)
from suiteharness.execution.protocols import ExecutionStrategies, ToolCallContext, Workflow  # noqa: E402
from suiteharness.execution.runner import ExecutionRunner  # noqa: E402
from suiteharness.runtime.effects import EffectScope  # noqa: E402
from suiteharness.runtime.kernel import ProductActivationContext  # noqa: E402
from suiteharness.runtime.scopes import RequestScope, ScopePath, ServiceBindings  # noqa: E402

from knowseam.harness_plugin import (  # noqa: E402
    CAPABILITY, KnowledgeHttpClient, KnowledgePluginConfig, KnowledgeTool, KnowledgeToolError,
    install_knowledge_tools, knowledge_tool_specs, register_knowledge_tools,
)

PATH = ScopePath.product("tenant-one", "feature-assistant")
SCOPE = RequestScope(PATH, "feishu-ou-actor1", channel_id="feishu")


class EmptyProductConfig(BaseModel):
    pass


def async_test(function):
    """Run this contract suite without requiring an async pytest plugin."""
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


def config(**kwargs):
    return KnowledgePluginConfig(**{
        "enabled": True, "base_url": "https://knowledge.internal",
        "service_token": "test-product-service-token", "company_by_tenant": {"tenant-one": "company-one"},
        **kwargs,
    })


def context(spec, *, scope=SCOPE, remaining_seconds=10):
    return ToolCallContext(scope, "run-test", "call-test", ToolIdentity.scoped(PATH, spec.name),
                           spec, remaining_seconds)


class Calls(Workflow):
    def __init__(self, calls):
        self.calls = calls

    async def next(self, request, frame, prompt):
        index = len(frame.observations)
        if index == len(self.calls):
            return WorkflowDecision.final({"done": True})
        name, arguments = self.calls[index]
        return WorkflowDecision.tools(ToolIntent(call_id=f"call-{index}", tool_name=name, arguments=arguments))


async def run_tools(registry, calls, *, grant_capability=True):
    authority = InMemoryCapabilityAuthority()
    identities = [registry.resolve(SCOPE, name).identity for name, _ in calls]
    grant = await authority.issue(PATH, tool_identities=identities,
                                  capabilities=[CAPABILITY] if grant_capability else [],
                                  principal_id=SCOPE.principal_id)
    runner = ExecutionRunner(tools=registry, capabilities=authority, approvals=InMemoryApprovalStore(),
                             journal=InMemoryAuditJournal())
    return await runner.run(RunRequest(run_id="run-e2e", grant_id=grant.grant_id, scope=SCOPE,
                                      input="查询企业知识", read_only=True),
                            ExecutionStrategies(workflow=Calls(calls)))


@async_test
async def test_real_registry_executor_list_search_read_and_cleanup():
    requests = []
    citation = {"document_id": "doc-1", "version_id": "version-7", "locator": {"sheet": "产品", "row": 3}}

    def response(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-product-service-token"
        assert request.headers["x-actor-id"] == SCOPE.principal_id
        assert request.headers["x-company-id"] == "company-one"
        assert request.headers["x-product-id"] == "feature-assistant"
        if request.url.path == "/v1/collections":
            return httpx.Response(200, json={"collections": [{"collection_id": "collection-1"}]})
        if request.url.path == "/v1/search":
            assert json.loads(request.content) == {"query": "报价", "collection_ids": ["collection-1"], "limit": 8}
            return httpx.Response(200, json={"hits": [{**citation, "text": "按审批流程报价"}]})
        assert request.url.path == "/v1/documents/doc-1"
        assert dict(request.url.params) == {"version_id": "version-7", "offset": "1", "limit": "2"}
        return httpx.Response(200, json={"document_id": "doc-1", "version_id": "version-7",
                                        "sections": [{"text": "原文", "locator": citation["locator"]}],
                                        "citations": [citation], "next_offset": None})

    registry = InMemoryToolRegistry()
    effects = EffectScope("knowledge-test")
    client = await register_knowledge_tools(registry, PATH, effects, config(), transport=httpx.MockTransport(response))
    try:
        result = await run_tools(registry, [
            ("knowledge.list", {}),
            ("knowledge.search", {"query": "报价", "collection_ids": ["collection-1"]}),
            ("knowledge.read", {"document_id": "doc-1", "version_id": "version-7", "offset": 1, "limit": 2}),
        ])
        assert len(requests) == 3
        assert all(item.status is ToolCallStatus.SUCCEEDED for item in result.observations)
        assert result.observations[-1].result["citations"] == [citation]
    finally:
        await effects.close()
    assert registry.resolve(SCOPE, "knowledge.list") is None
    assert client._http.is_closed


@async_test
async def test_activator_helper_uses_actual_activation_context():
    effects = EffectScope("activation-test")
    registry = InMemoryToolRegistry()
    activation = ProductActivationContext(PATH, EmptyProductConfig(), ServiceBindings.root(), effects, registry,
                                         frozenset({CAPABILITY}))
    await install_knowledge_tools(activation, config(),
                                 transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"collections": []})))
    result = await run_tools(registry, [("knowledge.list", {})])
    assert result.observations[0].status is ToolCallStatus.SUCCEEDED
    await effects.close()
    assert registry.resolve(SCOPE, "knowledge.read") is None


@async_test
async def test_no_capability_no_http_request():
    requests = []
    effects = EffectScope("denied")
    registry = InMemoryToolRegistry()
    await register_knowledge_tools(registry, PATH, effects, config(),
                                   transport=httpx.MockTransport(lambda request: requests.append(request)))
    try:
        result = await run_tools(registry, [("knowledge.list", {})], grant_capability=False)
        assert not requests
        assert not any(item.status is ToolCallStatus.SUCCEEDED for item in result.observations)
    finally:
        await effects.close()


@async_test
async def test_disabled_has_no_registration_or_client():
    effects = EffectScope("disabled")
    registry = InMemoryToolRegistry()
    assert await register_knowledge_tools(registry, PATH, effects, KnowledgePluginConfig()) is None
    assert await install_knowledge_tools(None, KnowledgePluginConfig()) is None
    assert registry.resolve(SCOPE, "knowledge.list") is None
    await effects.close()


def test_schema_is_strict_read_only_and_token_not_in_repr():
    specs = knowledge_tool_specs()
    for spec in specs:
        assert spec.effects == frozenset({ToolEffect.READ})
        assert spec.required_capabilities == frozenset({CAPABILITY})
        assert spec.input_schema["additionalProperties"] is False
        assert not {"actor_id", "company_id", "product_id", "base_url", "token"} & spec.input_schema["properties"].keys()
    assert "test-product-service-token" not in repr(config())
    specs[0].input_schema["properties"]["evil"] = {}
    assert knowledge_tool_specs()[0].input_schema["properties"] == {}


@pytest.mark.parametrize("name,arguments", [
    ("knowledge.list", {"actor_id": "other"}),
    ("knowledge.search", {"query": "x", "collection_ids": ["c"], "base_url": "https://evil"}),
    ("knowledge.search", {"query": "x", "collection_ids": ["c"], "limit": True}),
    ("knowledge.search", {"query": " ", "collection_ids": ["c"]}),
    ("knowledge.search", {"query": "x", "collection_ids": ["c", "c"]}),
    ("knowledge.search", {"query": "x", "collection_ids": []}),
    ("knowledge.read", {"document_id": "../secrets", "version_id": "v1"}),
    ("knowledge.read", {"document_id": "doc1"}),
    ("knowledge.read", {"document_id": "doc1", "version_id": "v1", "offset": -1}),
])
@async_test
async def test_extra_identity_and_invalid_arguments_fail_before_http(name, arguments):
    requests = []
    client = KnowledgeHttpClient(config(), PATH, transport=httpx.MockTransport(lambda req: requests.append(req)))
    spec = next(item for item in knowledge_tool_specs() if item.name == name)
    try:
        with pytest.raises(KnowledgeToolError, match="invalid knowledge tool arguments"):
            await KnowledgeTool(client, spec)(context(spec), arguments)
        assert not requests
    finally:
        await client.close()


@async_test
async def test_runner_also_rejects_forged_identity_arguments():
    registry = InMemoryToolRegistry()
    effects = EffectScope("invalid-schema")
    requests = []
    await register_knowledge_tools(registry, PATH, effects, config(),
                                   transport=httpx.MockTransport(lambda req: requests.append(req)))
    try:
        result = await run_tools(registry, [("knowledge.list", {"actor_id": "other"})])
        assert not requests
        assert result.observations[0].status is not ToolCallStatus.SUCCEEDED
    finally:
        await effects.close()


@pytest.mark.parametrize("response,expected", [
    (httpx.Response(302, headers={"location": "https://evil.invalid/collect"}), "redirects"),
    (httpx.Response(403, text="secret-token-sensitive-detail"), "access denied"),
    (httpx.Response(404), "unavailable"),
    (httpx.Response(500, text="secret-token-sensitive-detail"), "request failed"),
    (httpx.Response(200, text="<html>login</html>"), "invalid response"),
    (httpx.Response(200, content=b"{", headers={"content-type": "application/json"}), "invalid response"),
    (httpx.Response(200, json=[]), "invalid response"),
    (httpx.Response(200, content=b'{"value":NaN}', headers={"content-type": "application/json"}), "invalid response"),
    (httpx.Response(200, json={"content": "x" * 2000}), "size limit"),
])
@async_test
async def test_error_responses_bounded_no_redirects_or_error_body_leaks(response, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return response

    client = KnowledgeHttpClient(config(max_response_bytes=1024), PATH, transport=httpx.MockTransport(handler))
    spec = knowledge_tool_specs()[0]
    try:
        with pytest.raises(KnowledgeToolError, match=expected) as caught:
            await KnowledgeTool(client, spec)(context(spec), {})
        assert "sensitive-detail" not in str(caught.value)
        assert "test-product-service-token" not in str(caught.value)
        assert len(requests) == 1
    finally:
        await client.close()


@async_test
async def test_whole_request_deadline_is_enforced_even_for_slow_transport():
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"collections": []})

    client = KnowledgeHttpClient(config(), PATH, transport=httpx.MockTransport(slow))
    spec = knowledge_tool_specs()[0]
    try:
        with pytest.raises(KnowledgeToolError, match="timed out"):
            await KnowledgeTool(client, spec)(context(spec, remaining_seconds=0.01), {})
    finally:
        await client.close()


@async_test
async def test_unknown_company_and_cross_product_identity_fail_closed():
    requests = []
    spec = knowledge_tool_specs()[0]
    client = KnowledgeHttpClient(config(company_by_tenant={"other": "company-other"}), PATH,
                                 transport=httpx.MockTransport(lambda req: requests.append(req)))
    try:
        with pytest.raises(KnowledgeToolError, match="mapping"):
            await KnowledgeTool(client, spec)(context(spec), {})
        scope = RequestScope(ScopePath.product("tenant-one", "sales-assistant"), "actor")
        with pytest.raises(KnowledgeToolError, match="scope mismatch"):
            await KnowledgeTool(client, spec)(context(spec, scope=scope), {})
        assert not requests
    finally:
        await client.close()


@async_test
async def test_explicit_equal_tenant_mapping_and_agent_scope():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"collections": []})

    client = KnowledgeHttpClient(config(company_by_tenant={}, company_equals_tenant=True), PATH,
                                 transport=httpx.MockTransport(handler))
    scope = RequestScope(ScopePath.agent("tenant-one", "feature-assistant", "agent", "session"), "actor2")
    spec = knowledge_tool_specs()[0]
    try:
        await KnowledgeTool(client, spec)(context(spec, scope=scope), {})
        assert requests[0].headers["x-company-id"] == "tenant-one"
        assert requests[0].headers["x-actor-id"] == "actor2"
    finally:
        await client.close()


@async_test
async def test_read_rejects_wrong_version_instead_of_silent_replacement():
    client = KnowledgeHttpClient(config(), PATH, transport=httpx.MockTransport(lambda _: httpx.Response(200,
        json={"document_id": "doc1", "version_id": "new-version", "sections": [], "citations": []})))
    spec = knowledge_tool_specs()[2]
    try:
        with pytest.raises(KnowledgeToolError, match="version"):
            await KnowledgeTool(client, spec)(context(spec), {"document_id": "doc1", "version_id": "old-version"})
    finally:
        await client.close()


@async_test
async def test_partial_registration_failure_rolls_back_only_owned_tools():
    registry = InMemoryToolRegistry()
    existing_spec = knowledge_tool_specs()[1]
    existing = registry.register(PATH, existing_spec, lambda *args: None)
    effects = EffectScope("rollback")
    with pytest.raises(ValueError, match="already registered"):
        await register_knowledge_tools(registry, PATH, effects, config())
    assert registry.resolve(SCOPE, "knowledge.list") is None
    assert registry.resolve(SCOPE, "knowledge.search").identity == existing.identity
    await effects.close()
    existing.close()


@pytest.mark.parametrize("overrides", [
    {"base_url": "https://user:password@knowledge.internal"},
    {"base_url": "https://knowledge.internal/?token=secret"},
    {"base_url": "https://knowledge.internal/path"},
    {"base_url": "http://knowledge.internal"},
    {"base_url": "https://knowledge.internal:bad"},
    {"service_token": "secret\r\nX-Actor-Id:other"},
    {"company_by_tenant": {}},
    {"company_by_tenant": {"tenant-one": "invalid\ncompany"}},
    {"timeout_seconds": float("inf")},
    {"max_response_bytes": True},
])
def test_unsafe_configuration_rejected(overrides):
    with pytest.raises(ValueError):
        config(**overrides)
