"""Read-only authenticated retrieval API; no sync/source-management endpoints."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import uuid
from collections.abc import Sequence
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import ID_PATTERN, Identifier, ServiceClient
from .domain import AccessDenied, NotFound, Principal

MAX_BODY_BYTES = 64 * 1024
LOGGER = logging.getLogger("knowseam.api")
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    query: Annotated[str, Field(min_length=1, max_length=4096, pattern=r"\S")]
    collection_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)] | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 8

    @field_validator("collection_ids")
    @classmethod
    def no_duplicates(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("duplicate collection identifiers")
        return value


def _error(status: int, code: str) -> JSONResponse:
    error_id = uuid.uuid4().hex
    # Deliberately no request body, URL/query, identity, exception string or stack.
    LOGGER.warning("knowledge_api_error error_id=%s category=%s", error_id, code)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return JSONResponse({"error": code, "error_id": error_id}, status_code=status, headers=headers)


class _Boundary(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            # Do not rethrow: the ASGI server otherwise logs exception strings
            # which can contain SQL parameters, credentials or source content.
            response = _error(500, "internal_error")
        response.headers.update(SECURITY_HEADERS)
        return response


def _one_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise HTTPException(401)
    return values[0]


def _principal(request: Request, clients: tuple[ServiceClient, ...]) -> Principal:
    authorization = _one_header(request, "authorization")
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not 32 <= len(parts[1]) <= 512:
        raise HTTPException(401)
    supplied = parts[1].encode("utf-8")
    matched = None
    for client in clients:
        if hmac.compare_digest(supplied, client.token.encode("ascii")):
            matched = client
    if matched is None:
        raise HTTPException(401)
    actor = _one_header(request, "x-actor-id")
    company = _one_header(request, "x-company-id")
    product = _one_header(request, "x-product-id")
    if not re.fullmatch(ID_PATTERN, actor) or not re.fullmatch(ID_PATTERN, company) or not re.fullmatch(ID_PATTERN, product):
        raise HTTPException(403)
    if company != matched.company_id or product != matched.product_id:
        raise HTTPException(403)
    if matched.actor_ids and actor not in matched.actor_ids:
        raise HTTPException(403)
    return Principal(actor, company, product, matched.allowed_collections)


def _query(request: Request, allowed: set[str]) -> dict[str, str]:
    items = request.query_params.multi_items()
    keys = [key for key, _ in items]
    if len(keys) != len(set(keys)) or not set(keys) <= allowed:
        raise HTTPException(400)
    return dict(items)


def _no_content_encoding(request: Request) -> None:
    encodings = request.headers.getlist("content-encoding")
    if len(encodings) > 1 or (encodings and encodings[0].lower() not in {"", "identity"}):
        raise HTTPException(415)


async def _body(request: Request, *, limit: int = MAX_BODY_BYTES) -> bytes:
    _no_content_encoding(request)
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise HTTPException(400)
    if lengths:
        if not re.fullmatch(r"[0-9]+", lengths[0]) or len(lengths[0]) > 10:
            raise HTTPException(400)
        if int(lengths[0]) > limit:
            raise HTTPException(413 if limit else 400)
    data = bytearray()
    try:
        async with asyncio.timeout(15):
            async for part in request.stream():
                if len(data) + len(part) > limit:
                    raise HTTPException(413 if limit else 400)
                data.extend(part)
    except TimeoutError:
        raise HTTPException(408) from None
    return bytes(data)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _integer_parameter(value: str, maximum: int, *, minimum: int = 0) -> int:
    if len(value) > 10 or not re.fullmatch(r"[0-9]+", value):
        raise HTTPException(400)
    number = int(value)
    if not minimum <= number <= maximum:
        raise HTTPException(400)
    return number


def create_app(service: Any, clients: Sequence[ServiceClient]) -> Starlette:
    """Install routes over a separately constructed KnowledgeService instance."""
    trusted_clients = tuple(clients)
    if not all(isinstance(client, ServiceClient) for client in trusted_clients):
        raise ValueError("clients must be validated ServiceClient instances")
    if len({client.token for client in trusted_clients}) != len(trusted_clients):
        raise ValueError("client tokens must be unique")

    async def collections(request: Request):
        principal = _principal(request, trusted_clients)
        _query(request, set())
        await _body(request, limit=0)
        return JSONResponse({"collections": await service.collections(principal)})

    async def search(request: Request):
        principal = _principal(request, trusted_clients)
        _query(request, set())
        content_types = request.headers.getlist("content-type")
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415)
        raw = await _body(request)
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            body = SearchBody.model_validate(value)
        except (ValueError, UnicodeError, RecursionError, ValidationError):
            raise HTTPException(400) from None
        if body.collection_ids is not None and not set(body.collection_ids) <= set(principal.allowed_collections):
            raise AccessDenied()
        result = await service.search(principal, body.query, body.collection_ids, body.limit)
        return JSONResponse(result)

    async def read(request: Request):
        principal = _principal(request, trusted_clients)
        query = _query(request, {"version_id", "offset", "limit"})
        await _body(request, limit=0)
        document_id = request.path_params["document_id"]
        version_id = query.get("version_id", "")
        if not re.fullmatch(ID_PATTERN, document_id) or not re.fullmatch(ID_PATTERN, version_id):
            raise HTTPException(400)
        offset = _integer_parameter(query.get("offset", "0"), 1000000)
        limit = _integer_parameter(query.get("limit", "50"), 100, minimum=1)
        result = await service.read(principal, document_id, version_id, offset=offset, limit=limit)
        return JSONResponse(result)

    async def live(request: Request):
        _query(request, set())
        await _body(request, limit=0)
        return JSONResponse({"status": "ok"})

    async def ready(request: Request):
        _query(request, set())
        await _body(request, limit=0)
        try:
            available = await service.ready() is True
        except Exception:
            return _error(503, "not_ready")
        if not available:
            return _error(503, "not_ready")
        return JSONResponse({"status": "ready"})

    async def not_found(request, exception):
        return _error(404, "not_found")

    async def http_error(request, exception):
        code = {
            400: "invalid_request", 401: "authentication_required", 403: "access_denied",
            404: "not_found", 405: "method_not_allowed", 408: "request_timeout",
            413: "request_too_large", 415: "unsupported_media_type",
        }.get(exception.status_code, "request_failed")
        return _error(exception.status_code, code)

    app = Starlette(debug=False, routes=[
        Route("/v1/collections", collections, methods=["GET"]),
        Route("/v1/search", search, methods=["POST"]),
        Route("/v1/documents/{document_id}", read, methods=["GET"]),
        Route("/health/live", live, methods=["GET"]),
        Route("/health/ready", ready, methods=["GET"]),
    ], exception_handlers={AccessDenied: not_found, NotFound: not_found, HTTPException: http_error})
    app.router.redirect_slashes = False
    app.add_middleware(_Boundary)
    return app
