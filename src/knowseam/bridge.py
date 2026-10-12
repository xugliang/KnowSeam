"""Optional stdio-to-remote-MCP bridge: no database, indexing or inference here."""
import argparse
import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, ListResourceTemplatesResult, TextContent
from pydantic import SecretStr, model_validator

from .mcp_server import ConfigModel

ALLOWED_TOOLS = frozenset({"knowledge_list", "knowledge_search", "knowledge_read"})
EXTENDED_TOOLS = ALLOWED_TOOLS | frozenset({
    'knowledge_capabilities','knowledge_catalog','knowledge_context','knowledge_table_list','knowledge_table_read',
    'knowledge_history_search','knowledge_history_read','knowledge_visual_search'})
ASSET_TEMPLATE = 'knowseam://assets/{asset_id}/{version_id}'


class BridgeConfig(ConfigModel):
    url: str
    token: SecretStr
    allow_insecure_http: bool = False
    extended_tools: bool = False

    @model_validator(mode="after")
    def validate_connection(self):
        url = urlsplit(self.url)
        _ = url.port
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment or "\\" in self.url or any(c.isspace() for c in self.url)):
            raise ValueError("invalid remote MCP URL")
        if url.scheme == "http" and not self.allow_insecure_http:
            raise ValueError("HTTP requires trusted network opt-in")
        token = self.token.get_secret_value()
        if not 32 <= len(token) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("invalid token")
        if token.startswith("REPLACE"):
            raise ValueError("example token must be replaced")
        return self


def bridge_server(client, *, extended=False):
    allowed = EXTENDED_TOOLS if extended else ALLOWED_TOOLS
    async def list_tools(context, params):
        result = await client.list_tools()
        return ListToolsResult(tools=[t for t in result.tools if t.name in allowed])

    async def call_tool(context, params):
        if params.name not in allowed:
            return CallToolResult(is_error=True, content=[TextContent(type="text", text="Unknown knowledge tool")])
        try:
            return await client.call_tool(params.name, params.arguments or {})
        except Exception:
            return CallToolResult(is_error=True, content=[TextContent(type="text", text="Knowledge service unavailable")])

    async def templates(context, params):
        result = await client.list_resource_templates(cache_mode='bypass')
        return ListResourceTemplatesResult(resource_templates=[
            item for item in result.resource_templates if str(item.uri_template)==ASSET_TEMPLATE])

    async def read_resource(context, params):
        from .identifiers import ID_PATTERN
        uri = str(params.uri)
        parts = uri.removeprefix('knowseam://assets/').split('/')
        if not uri.startswith('knowseam://assets/') or len(parts)!=2 or not all(re.fullmatch(ID_PATTERN,p) for p in parts):
            raise ValueError('Unknown knowledge resource')
        result = await client.read_resource(uri,cache_mode='bypass')
        if len(result.contents)!=1 or str(result.contents[0].uri)!=uri or result.contents[0].mime_type!='image/png':
            raise ValueError('Invalid knowledge resource')
        if not getattr(result.contents[0],'blob',None) or len(result.contents[0].blob)>14*1024*1024:
            raise ValueError('Invalid knowledge resource')
        return result

    return Server("KnowSeam bridge", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool,
                  on_list_resource_templates=templates if extended else None,
                  on_read_resource=read_resource if extended else None)


async def run(config):
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + config.token.get_secret_value()},
                                  trust_env=False, follow_redirects=False, timeout=45) as http:
        async with Client(streamable_http_client(config.url, http_client=http), read_timeout_seconds=60) as client:
            server = bridge_server(client, extended=config.extended_tools)
            async with stdio_server() as streams:
                await server.run(streams[0], streams[1], server.create_initialization_options())


def main(argv=None):
    parser = argparse.ArgumentParser(description="KnowSeam remote-only stdio bridge")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = BridgeConfig.model_validate_json(Path(args.config).read_text(encoding="utf-8"))
        asyncio.run(run(config))
    except KeyboardInterrupt:
        return 130
    except Exception:
        parser.exit(1, "KnowSeam bridge unavailable; check private configuration and server connectivity.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
