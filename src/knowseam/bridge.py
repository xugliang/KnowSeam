"""Optional stdio-to-remote-MCP bridge: no database, indexing or inference here."""
import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent
from pydantic import SecretStr, model_validator

from .mcp_server import ConfigModel

ALLOWED_TOOLS = frozenset({"knowledge_list", "knowledge_search", "knowledge_read"})


class BridgeConfig(ConfigModel):
    url: str
    token: SecretStr
    allow_insecure_http: bool = False

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


def bridge_server(client):
    async def list_tools(context, params):
        result = await client.list_tools()
        return ListToolsResult(tools=[t for t in result.tools if t.name in ALLOWED_TOOLS])

    async def call_tool(context, params):
        if params.name not in ALLOWED_TOOLS:
            return CallToolResult(is_error=True, content=[TextContent(type="text", text="Unknown knowledge tool")])
        try:
            return await client.call_tool(params.name, params.arguments or {})
        except Exception:
            return CallToolResult(is_error=True, content=[TextContent(type="text", text="Knowledge service unavailable")])

    return Server("KnowSeam bridge", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool)


async def run(config):
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + config.token.get_secret_value()},
                                  trust_env=False, follow_redirects=False, timeout=45) as http:
        async with Client(streamable_http_client(config.url, http_client=http), read_timeout_seconds=60) as client:
            server = bridge_server(client)
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
