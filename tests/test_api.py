"""API boundary tests with a service double, not real database/model E2E."""

import asyncio
from functools import wraps

import httpx
import pytest

from knowseam.api import MAX_BODY_BYTES, SECURITY_HEADERS, create_app
from knowseam.config import ServiceClient
from knowseam.domain import AccessDenied, NotFound

TOKEN = "api-contract-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CLIENT = ServiceClient(TOKEN, "feature-assistant", "company-one", ("public",), ("employee-1",))
HEADERS = {"Authorization": "Bearer " + TOKEN, "X-Actor-Id": "employee-1",
           "X-Company-Id": "company-one", "X-Product-Id": "feature-assistant"}


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class Service:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.available = True

    def check(self):
        if self.failure:
            raise self.failure

    async def collections(self, principal):
        self.check()
        self.calls.append(("collections", principal))
        return [{"id": "public", "title": "公共资料"}]

    async def search(self, principal, query, collection_ids, limit):
        self.check()
        self.calls.append(("search", principal, query, collection_ids, limit))
        return {"hits": [{"document_id": "doc-1", "version_id": "version-1", "text": "查询证据"}]}

    async def read(self, principal, document_id, version_id, offset=0, limit=50):
        self.check()
        self.calls.append(("read", principal, document_id, version_id, offset, limit))
        return {"document_id": document_id, "version_id": version_id, "sections": [{"text": "原文"}],
                "citations": [{"document_id": document_id, "version_id": version_id}]}

    async def ready(self):
        self.check()
        return self.available


def client(service=None, *, service_clients=(CLIENT,)):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service or Service(), service_clients)),
                             base_url="http://testserver", headers=HEADERS)


@async_test
async def test_three_routes_build_principal_and_preserve_versions():
    service = Service()
    async with client(service) as http:
        listed = await http.get("/v1/collections")
        searched = await http.post("/v1/search", json={"query": "产品有什么功能", "collection_ids": ["public"]})
        read = await http.get("/v1/documents/doc-1?version_id=version-1&offset=2&limit=10")
    assert listed.status_code == searched.status_code == read.status_code == 200
    assert listed.json()["collections"][0]["id"] == "public"
    assert searched.json()["hits"][0]["version_id"] == "version-1"
    assert read.json()["citations"] == [{"document_id": "doc-1", "version_id": "version-1"}]
    principal = service.calls[1][1]
    assert (principal.actor_id, principal.company_id, principal.product_id, principal.allowed_collections) == (
        "employee-1", "company-one", "feature-assistant", ("public",))
    assert service.calls[1][4] == 8
    assert service.calls[2][4:] == (2, 10)
    for header, value in SECURITY_HEADERS.items():
        assert read.headers[header] == value
    assert "access-control-allow-origin" not in read.headers


@pytest.mark.parametrize("overrides", [
    {"Authorization": "Bearer " + "x" * 40},
    {"Authorization": "Basic " + TOKEN},
    {"Authorization": ""},
    {"X-Actor-Id": ""},
    {"X-Actor-Id": "other-employee"},
    {"X-Actor-Id": "x" * 129},
    {"X-Company-Id": "other-company"},
    {"X-Product-Id": "sales-assistant"},
])
@async_test
async def test_identity_spoofing_rejected_before_service(overrides):
    service = Service()
    async with client(service) as http:
        response = await http.get("/v1/collections", headers=overrides)
    assert response.status_code in {401, 403}
    assert not service.calls
    assert TOKEN not in response.text


@async_test
async def test_actor_list_empty_explicitly_trusts_backend_authenticated_actor():
    service = Service()
    trusted = ServiceClient(TOKEN, "feature-assistant", "company-one", ("public",))
    async with client(service, service_clients=(trusted,)) as http:
        response = await http.get("/v1/collections", headers={"X-Actor-Id": "new-employee"})
    assert response.status_code == 200
    assert service.calls[0][1].actor_id == "new-employee"


@async_test
async def test_duplicate_auth_header_rejected():
    service = Service()
    async with client(service) as http:
        headers = [(key, value) for key, value in HEADERS.items()]
        headers.append(("Authorization", "Bearer " + TOKEN))
        response = await http.get("/v1/collections", headers=headers)
    assert response.status_code == 401
    assert not service.calls


@pytest.mark.parametrize("body", [
    {"query": "x", "actor_id": "other"}, {"query": "x", "company_id": "other"},
    {"query": "x", "product_id": "other"}, {"query": "x", "base_url": "https://evil"},
    {"query": "x", "limit": True}, {"query": "x", "limit": "2"}, {"query": "x", "limit": 21},
    {"query": " "}, {"query": "x" * 4097}, {"query": 123}, {"query": "x", "collection_ids": []},
    {"query": "x", "collection_ids": ["public", "public"]}, [],
])
@async_test
async def test_unknown_fields_and_bad_search_body_rejected(body):
    service = Service()
    async with client(service) as http:
        response = await http.post("/v1/search", json=body)
    assert response.status_code == 400
    assert not service.calls


@pytest.mark.parametrize("url", [
    "/v1/collections?token=secret", "/v1/collections?actor_id=other", "/v1/documents/doc-1",
    "/v1/documents/doc-1?version_id=v1&version_id=v2", "/v1/documents/doc-1?version_id=v1&actor_id=other",
    "/v1/documents/doc-1?version_id=v1&offset=-1", "/v1/documents/doc-1?version_id=v1&limit=101",
    "/v1/documents/doc-1?version_id=v1&offset=1000001", "/v1/documents/doc-1?version_id=v1&limit=true",
])
@async_test
async def test_unknown_duplicate_or_invalid_query_fields_rejected(url):
    service = Service()
    async with client(service) as http:
        response = await http.get(url)
    assert response.status_code == 400
    assert not service.calls


@async_test
async def test_forbidden_collection_and_not_found_are_indistinguishable():
    service = Service()
    async with client(service) as http:
        forbidden = await http.post("/v1/search", json={"query": "x", "collection_ids": ["private"]})
        assert not service.calls
        service.failure = NotFound("sensitive-title")
        absent = await http.get("/v1/documents/doc-1?version_id=v1")
        service.failure = AccessDenied("sensitive-title")
        denied = await http.get("/v1/documents/doc-1?version_id=v1")
    for response in (forbidden, absent, denied):
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"
        assert "sensitive" not in response.text


@async_test
async def test_body_length_prechecked_and_streamed_body_bounded():
    service = Service()
    async with client(service) as http:
        response = await http.post("/v1/search", content=b"{}",
                                   headers={"content-type": "application/json", "content-length": str(MAX_BODY_BYTES + 1)})
        assert response.status_code == 413

        async def chunks():
            yield b" " * 32768
            yield b" " * 32768
            yield b"{}"

        response = await http.post("/v1/search", content=chunks(), headers={"content-type": "application/json"})
        assert response.status_code == 413
        response = await http.request("GET", "/v1/collections", content=b'{"actor_id":"other"}')
        assert response.status_code == 400
    assert not service.calls


@pytest.mark.parametrize("raw", [b'{"query":"a","query":"b"}', b'{"query":NaN}', b"{", b"\xff"])
@async_test
async def test_nonstandard_json_rejected(raw):
    async with client() as http:
        response = await http.post("/v1/search", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 400


@async_test
async def test_media_type_encoding_and_search_query_rejected():
    async with client() as http:
        assert (await http.post("/v1/search", content="query=x")).status_code == 415
        assert (await http.post("/v1/search", json={"query": "x"}, headers={"content-encoding": "gzip"})).status_code == 415
        assert (await http.post("/v1/search?token=secret", json={"query": "x"})).status_code == 400


@async_test
async def test_health_truthful_no_details_and_internal_errors_sanitized(caplog):
    service = Service()
    async with client(service) as http:
        assert (await http.get("/health/live")).json() == {"status": "ok"}
        assert (await http.get("/health/ready")).json() == {"status": "ready"}
        service.available = False
        assert (await http.get("/health/ready")).status_code == 503
        service.failure = RuntimeError("SELECT password FROM secret_table; " + TOKEN)
        unavailable = await http.get("/health/ready")
        failed = await http.post("/v1/search", json={"query": "confidential-query"})
        assert unavailable.status_code == 503
        assert failed.status_code == 500
        assert failed.json()["error_id"]
        assert failed.headers["cache-control"] == "no-store"
    for forbidden in (TOKEN, "secret_table", "confidential-query", "SELECT password"):
        assert forbidden not in failed.text + unavailable.text + caplog.text


@async_test
async def test_no_write_endpoints_no_cors_and_no_slash_redirect():
    async with client() as http:
        assert (await http.post("/v1/sync", json={})).status_code == 404
        assert (await http.delete("/v1/documents/doc-1")).status_code == 405
        assert (await http.get("/v1/collections/")).status_code == 404
        preflight = await http.options("/v1/search", headers={"Origin": "https://evil.invalid",
                                                               "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in preflight.headers


def test_repeated_service_token_rejected_at_app_creation():
    with pytest.raises(ValueError, match="unique"):
        create_app(Service(), [CLIENT, CLIENT])
