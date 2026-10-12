import sys

import httpx
import pytest

from knowseam.domain import ParsedDocument, Section, UnsupportedDocument
from knowseam.document_parsing.protocol import ParserIdentity, ParseMetadata, encode_metadata, encode_document
from knowseam.tables.native import from_rows


@pytest.mark.asyncio
async def test_executor_explicit_versions_and_subprocess_roundtrip(tmp_path):
    from knowseam.document_parsing.executor import ExecutorSettings, create_executor_app
    from knowseam.document_parsing.process_runner import ProcessRunner

    table = from_rows("t-1", [["code"], ["001"]])
    doc = ParsedDocument((Section("source"),), parser_version="v", tables=(table,), structure_version=2)
    output = encode_document(doc, protocol_version=2).decode()
    code = (
        "import json,base64,sys; m=json.loads(base64.b64decode(sys.stdin.buffer.readline())); assert m['protocol_version']==2; print("
        + repr(output)
        + ")"
    )
    runner = ProcessRunner(tmp_path, command=(sys.executable, "-c", code))
    identity = ParserIdentity(
        protocol_version=2,
        upstream_sha="a" * 40,
        runtime_source_sha256="b" * 64,
        dependency_fingerprint="c" * 64,
        model_fingerprint="d" * 64,
    )
    app = create_executor_app(ExecutorSettings(tmp_path / "p.sock", tmp_path, identity), runner=runner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://parser") as client:
        assert (await client.get("/v1/status")).json()["protocol_version"] == 1
        assert (await client.get("/v2/status")).json()["protocol_version"] == 2
        result = await client.post(
            "/v2/parse",
            content=b"x",
            headers={
                "X-KnowSeam-Parse-Metadata": encode_metadata(
                    ParseMetadata(filename="x.txt", media_type="text/plain")
                )
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["tables"][0]["cells"][1]["value"] == "001"


@pytest.mark.parametrize("status", [404, 422, 500])
def test_negotiation_fallback_only_on_explicit_unsupported(tmp_path, status):
    from knowseam.document_parsing.client import ParserClient

    calls = []
    identity = ParserIdentity(
        upstream_sha="a" * 40,
        runtime_source_sha256="b" * 64,
        dependency_fingerprint="c" * 64,
        model_fingerprint="d" * 64,
    )

    def response(request):
        calls.append(request.url.path)
        return httpx.Response(status if request.url.path == "/v2/status" else 200, json=identity.model_dump())

    client = ParserClient(tmp_path / "p.sock")
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(response), base_url="http://parser")
    try:
        if status == 404:
            assert client.status().protocol_version == 1
            assert calls == ["/v2/status", "/v1/status"]
        else:
            with pytest.raises(UnsupportedDocument):
                client.status()
            assert calls == ["/v2/status"]
    finally:
        client.close()
