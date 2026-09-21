"""Full MCP/HTTP -> authenticated REST -> service boundary, no LLM or Feishu writes."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import socket
import sys
import json

import httpx
import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import uvicorn

from knowseam.api import create_app as rest_app
from knowseam.config import ServiceClient
from knowseam.mcp_server import GatewayConfig, create_app

USER_TOKEN = "a" * 48
OTHER_TOKEN = "b" * 48
UPSTREAM_TOKEN = "c" * 48


class Knowledge:
    def __init__(self):
        self.calls = []

    async def collections(self, principal):
        self.calls.append(("list", principal))
        return [{"id": "public"}, {"id": "restricted"}]

    async def search(self, principal, query, collection_ids, limit):
        await asyncio.sleep(0)
        self.calls.append(("search", principal, query, collection_ids))
        return {"hits": [{"document_id": "doc", "version_id": "v1", "collection_id": "public",
                          "text": "公司制度", "actor": principal.actor_id}], "evidence_status": "candidates"}

    async def read(self, principal, document_id, version_id, offset, limit):
        self.calls.append(("read", principal, document_id, version_id, offset, limit))
        return {"document_id": document_id, "version_id": version_id,
                "collection_id": "restricted" if document_id == "private" else "public",
                "sections": [{"text": "内容"}], "citations": []}


def configuration(**updates):
    return GatewayConfig.model_validate({
        "upstream_url": "http://knowledge", "upstream_token": UPSTREAM_TOKEN,
        "allow_insecure_http": True, "allowed_hosts": ["testserver", "127.0.0.1:*"],
        "bindings": [{"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                      "actor_id": actor, "company_id": "company", "product_id": "agent",
                      "allowed_collections": ["public"]} for token, actor in [(USER_TOKEN, "alice"), (OTHER_TOKEN, "bob")]],
        **updates,
    })


def application(service=None):
    service = service or Knowledge()
    api = rest_app(service, [ServiceClient(token=UPSTREAM_TOKEN, company_id="company", product_id="agent",
                                          allowed_collections=("public", "restricted"), actor_ids=("alice", "bob"))])
    return create_app(configuration(), transport=httpx.ASGITransport(api)), service


@asynccontextmanager
async def http_client(app, token=USER_TOKEN):
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver",
                                     headers={"Authorization": "Bearer " + token,
                                              "Accept": "application/json, text/event-stream",
                                              "MCP-Protocol-Version": "2025-06-18"}) as client:
            yield client


async def rpc(client, method, params=None, **kwargs):
    return await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                          "params": params or {}}, **kwargs)


async def call(client, name, arguments):
    response = await rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


@pytest.mark.asyncio
async def test_legacy_wire_lifecycle_and_readonly_schema():
    app, service = application()
    async with http_client(app) as client:
        init = await rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "test", "version": "1"}})
        assert init.status_code == 200
        tools = (await rpc(client, "tools/list")).json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"knowledge_list", "knowledge_search", "knowledge_read"}
        for tool in tools:
            assert tool["annotations"]["readOnlyHint"]
            assert not {"actor_id", "company_id", "token", "url"} & tool["inputSchema"]["properties"].keys()
        result = await call(client, "knowledge_list", {})
        assert result["structuredContent"] == {"collections": [{"id": "public"}]}
        result = await call(client, "knowledge_search", {"query": "制度", "collection_ids": ["public"]})
        assert result["structuredContent"]["hits"][0]["actor"] == "alice"
        result = await call(client, "knowledge_read", {"document_id": "doc", "version_id": "v1", "offset": 1})
        assert result["structuredContent"]["sections"]
        assert service.calls[-1][-2:] == (1, 50)


@pytest.mark.asyncio
async def test_auth_scope_spoofing_and_parallel_identity():
    app, service = application()
    async with http_client(app) as client:
        assert (await rpc(client, "tools/list", headers={"Authorization": "Bearer bad"})).status_code == 401
        denied = await call(client, "knowledge_search", {"query": "制度", "collection_ids": ["restricted"]})
        assert denied["structuredContent"]["error"] == "unavailable"
        assert not service.calls
        result = await rpc(client, "tools/call", {"name": "knowledge_search", "arguments": {
            "query": "制度", "collection_ids": ["public"]}}, headers={"X-Actor-Id": "admin"})
        assert result.json()["result"]["structuredContent"]["hits"][0]["actor"] == "alice"
        denied = await call(client, "knowledge_read", {"document_id": "private", "version_id": "v1"})
        assert denied["structuredContent"] == {"error": "unavailable"}
        async def one(token):
            result = await rpc(client, "tools/call", {"name": "knowledge_search", "arguments": {
                "query": "制度", "collection_ids": ["public"]}}, headers={"Authorization": "Bearer " + token})
            return result.json()["result"]["structuredContent"]["hits"][0]["actor"]
        assert await asyncio.gather(one(USER_TOKEN), one(OTHER_TOKEN), one(USER_TOKEN)) == ["alice", "bob", "alice"]


@pytest.mark.asyncio
async def test_network_and_argument_guards():
    app, service = application()
    async with http_client(app) as client:
        assert (await rpc(client, "tools/list", headers={"Host": "attacker.invalid"})).status_code == 421
        assert (await rpc(client, "tools/list", headers={"Origin": "https://attacker.invalid"})).status_code == 403
        assert (await client.post("/mcp", content=b"x" * 65537)).status_code == 413
        for args in [dict(query="", collection_ids=["public"]), dict(query="a", collection_ids=["public"], limit=1000)]:
            result = await call(client, "knowledge_search", args)
            assert result.get("isError")
        assert not service.calls


@pytest.mark.asyncio
async def test_sdk_real_tcp_end_to_end():
    app, _ = application()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + USER_TOKEN}, trust_env=False) as http:
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) == 3
                    result = await session.call_tool("knowledge_search", {"query": "制度", "collection_ids": ["public"]})
                    assert result.structured_content["hits"][0]["actor"] == "alice"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@pytest.mark.asyncio
async def test_stdio_bridge_to_remote_tcp(tmp_path):
    app, service = application()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    config_file = tmp_path / "client.json"
    config_file.write_text(json.dumps({"url": f"http://127.0.0.1:{port}/mcp", "token": USER_TOKEN,
                                      "allow_insecure_http": True}), encoding="utf-8")
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.01)
        async with Client(StdioServerParameters(command=sys.executable, args=[
            "-m", "knowseam.bridge", "--config", str(config_file)]), read_timeout_seconds=15) as client:
            assert len((await client.list_tools()).tools) == 3
            result = await client.call_tool("knowledge_search", {"query": "制度", "collection_ids": ["public"]})
            assert result.structured_content["hits"][0]["actor"] == "alice"
            denied = await client.call_tool("arbitrary_sql", {"query": "DROP TABLE"})
            assert denied.is_error
        assert service.calls
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(302, headers={"location": "http://attacker.invalid"}),
                                      httpx.Response(500, text="private upstream details"),
                                      httpx.Response(200, json={"x": "a" * 600000}),
                                      httpx.Response(200, text="not JSON")])
async def test_upstream_failures_are_bounded(response):
    requests = []
    def handler(request):
        requests.append(request)
        return response
    app = create_app(configuration(), transport=httpx.MockTransport(handler))
    async with http_client(app) as client:
        result = await call(client, "knowledge_list", {})
        assert "error" in result["structuredContent"]
        assert "private upstream details" not in json.dumps(result)
        assert len(requests) == 1


@pytest.mark.parametrize("updates", [{"upstream_url": "https://a/user"}, {"allowed_hosts": ["*"]},
                                      {"upstream_url": "http://a", "allow_insecure_http": False}])
def test_configuration_fails_closed(updates):
    with pytest.raises(ValueError):
        configuration(**updates)
