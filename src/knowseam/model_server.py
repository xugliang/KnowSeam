"""Loopback-first local Qwen model HTTP process; no downloads or synthetic outputs.

Run ``python -m knowseam.model_server --model-root /models``. The root
contains Qwen3-Embedding-0.6B and Qwen3-Reranker-0.6B directories. A non-loopback
bind requires --token-file, never a credential in command-line arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import importlib
import ipaddress
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .domain import Embedder, Reranker
from .models import (
    ModelError, Qwen3Embedder, Qwen3Reranker, query_instruction,
    validate_embeddings, validate_scores, validate_texts,
)

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_BATCH_INPUTS = 128
EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
RERANKING_MODEL = "Qwen/Qwen3-Reranker-0.6B"
LOGGER = logging.getLogger("knowseam.model_server")


def _integer(value: Any, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ModelError(f"{name} must be an integer between {low} and {high}")
    return value


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _validate_token(token: str) -> str:
    if (not isinstance(token, str) or not 32 <= len(token) <= 512
            or not all(33 <= ord(character) <= 126 for character in token)):
        raise ModelError("model token must contain 32 to 512 printable non-space ASCII characters")
    return token


def read_token(path: str | Path) -> str:
    """Snapshot one bounded token file; never echo its path or content."""
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(515)
        if len(data) > 514:
            raise ModelError("model token file exceeds size limit")
        # A single final newline is convenient for a file-based secret mount.
        return _validate_token(data.decode("ascii").removesuffix("\n").removesuffix("\r"))
    except (OSError, UnicodeError):
        raise ModelError("cannot read model token file") from None


@dataclass(frozen=True)
class ModelServerConfig:
    model_root: Path
    host: str = "127.0.0.1"
    port: int = 18792
    token_file: Path | None = None
    device: str = "cpu"
    dtype: str = "float32"
    threads: int = 2
    batch_size: int = 2
    max_length: int = 4096
    dimensions: int = 1024
    timeout: int = 120
    model_revision: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host or any(character.isspace() for character in self.host):
            raise ModelError("host must be a nonempty bind address")
        if not _loopback(self.host) and self.token_file is None:
            raise ModelError("non-loopback bind requires --token-file")
        if not isinstance(self.device, str) or not self.device.strip():
            raise ModelError("device is required")
        if self.dtype not in {"float32", "float16", "bfloat16"}:
            raise ModelError("unsupported model dtype")
        for name, low, high in [("port", 1, 65535), ("threads", 1, 32), ("batch_size", 1, 32),
                               ("max_length", 64, 32768), ("dimensions", 32, 4096), ("timeout", 1, 600)]:
            _integer(getattr(self, name), low, high, name)


def configure_threads(threads: int) -> None:
    """Limit this dedicated process only; never change host or GPU settings."""
    _integer(threads, 1, 32, "threads")
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[variable] = str(threads)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    try:
        torch = importlib.import_module("torch")
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
    except Exception:
        raise ModelError("cannot initialize bounded local model threads") from None


class _RequestError(Exception):
    def __init__(self, status: int, category: str):
        self.status, self.category = status, category


def _error(status: int, category: str) -> JSONResponse:
    # Do not log exception strings, request URLs, query text, bodies or headers.
    LOGGER.warning("model_server_error category=%s", category)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return JSONResponse({"error": {"type": category}}, status_code=status, headers=headers)


class _Boundary(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            response = _error(503, "internal_error")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def _authorize(request: Request, token: str | None) -> None:
    if request.query_params:
        raise _RequestError(400, "invalid_query")
    if token is None:
        return
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise _RequestError(401, "unauthorized")
    parts = values[0].split(" ")
    if (len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > 512
            or not hmac.compare_digest(parts[1].encode("utf-8"), token.encode("ascii"))):
        raise _RequestError(401, "unauthorized")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError("nonfinite number")


async def _body(request: Request) -> dict[str, Any]:
    content_types = request.headers.getlist("content-type")
    if (len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip().lower() != "application/json"):
        raise _RequestError(415, "unsupported_media_type")
    encodings = request.headers.getlist("content-encoding")
    if len(encodings) > 1 or (encodings and encodings[0].lower() not in {"", "identity"}):
        raise _RequestError(415, "unsupported_content_encoding")
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1 or (lengths and (len(lengths[0]) > 10 or not lengths[0].isascii()
                                       or not lengths[0].isdigit())):
        raise _RequestError(400, "invalid_content_length")
    if lengths and int(lengths[0]) > MAX_BODY_BYTES:
        raise _RequestError(413, "body_too_large")
    data = bytearray()
    try:
        async with asyncio.timeout(15):
            async for part in request.stream():
                if len(data) + len(part) > MAX_BODY_BYTES:
                    raise _RequestError(413, "body_too_large")
                data.extend(part)
    except TimeoutError:
        raise _RequestError(408, "request_timeout") from None
    if lengths and len(data) != int(lengths[0]):
        raise _RequestError(400, "invalid_content_length")
    try:
        payload = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise _RequestError(400, "invalid_json") from None
    if not isinstance(payload, dict):
        raise _RequestError(400, "invalid_body")
    return payload


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_BATCH_INPUTS:
        raise _RequestError(400, "invalid_batch")
    try:
        return validate_texts(value)
    except ModelError:
        raise _RequestError(400, "invalid_input") from None


def create_app(embedder: Embedder, reranker: Reranker, *, token: str | None = None) -> Starlette:
    """Install compatible endpoints over injected adapters, always with warmup."""
    if token is not None:
        _validate_token(token)

    async def warmup(app: Starlette) -> None:
        try:
            # A constructor or empty batch proves nothing about usable weights.
            vectors = await embedder.embed([query_instruction("如何查询公司产品资料？")], query=False)
            validate_embeddings(vectors, 1, embedder.dimensions)
            scores = await reranker.score("如何查询公司产品资料？", ["公司产品资料可以通过企业知识库检索。"])
            validate_scores(scores, 1)
        except asyncio.CancelledError:
            raise
        except Exception:
            app.state.ready = False
            app.state.warmup_status = "model_error"
            LOGGER.error("model_server_warmup category=model_error")
        else:
            app.state.ready = True
            app.state.warmup_status = "ready"

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.ready = False
        app.state.warmup_status = "warming_up"
        app.state.warmup_task = asyncio.create_task(warmup(app))
        try:
            yield
        finally:
            app.state.ready = False
            task = app.state.warmup_task
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def ready(request: Request):
        _authorize(request, token)
        if not request.app.state.ready:
            return _error(503, request.app.state.warmup_status)
        return JSONResponse({"status": "ready"})

    async def run_model(request: Request, operation):
        state = request.app.state
        if not state.ready:
            raise _RequestError(503, state.warmup_status)
        # One in-flight request across both models, not an unbounded GPU/CPU queue.
        if state.inference_lock.locked():
            raise _RequestError(503, "model_busy")
        async with state.inference_lock:
            try:
                return await operation()
            except asyncio.CancelledError:
                # A cancelled HTTP coroutine cannot stop an inference thread.
                # Do not start another model while that thread may still run.
                state.ready = False
                state.warmup_status = "inference_interrupted"
                raise
            except ModelError:
                state.ready = False
                state.warmup_status = "model_error"
                raise _RequestError(503, "model_error") from None
            except Exception:
                state.ready = False
                state.warmup_status = "inference_error"
                raise _RequestError(503, "inference_error") from None

    async def embeddings(request: Request):
        _authorize(request, token)
        payload = await _body(request)
        if (set(payload) - {"model", "input", "dimensions", "encoding_format"}
                or payload.get("model") != embedder.model_id):
            raise _RequestError(400, "invalid_model_or_fields")
        dimensions = payload.get("dimensions", embedder.dimensions)
        if type(dimensions) is not int or dimensions != embedder.dimensions:
            raise _RequestError(400, "unsupported_dimensions")
        if payload.get("encoding_format", "float") != "float":
            raise _RequestError(400, "unsupported_encoding")
        inputs = payload.get("input")
        texts = _texts([inputs] if isinstance(inputs, str) else inputs)

        async def infer():
            # HTTPEmbedder already wrapped queries; never add the instruction again.
            vectors = validate_embeddings(await embedder.embed(texts, query=False), len(texts), dimensions)
            return JSONResponse({"object": "list", "model": embedder.model_id, "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ]})

        return await run_model(request, infer)

    async def rerank(request: Request):
        _authorize(request, token)
        payload = await _body(request)
        if (set(payload) - {"model", "query", "documents", "top_n"}
                or payload.get("model") != reranker.model_id):
            raise _RequestError(400, "invalid_model_or_fields")
        query = _texts([payload.get("query")])[0]
        documents = _texts(payload.get("documents"))
        _texts([query + "\n" + document for document in documents])
        top_n = payload.get("top_n", len(documents))
        if type(top_n) is not int or not 1 <= top_n <= len(documents):
            raise _RequestError(400, "invalid_top_n")

        async def infer():
            scores = validate_scores(await reranker.score(query, documents), len(documents))
            ordered = sorted(range(len(scores)), key=lambda index: (-scores[index], index))[:top_n]
            return JSONResponse({"model": reranker.model_id, "results": [
                {"index": index, "relevance_score": scores[index]} for index in ordered
            ]})

        return await run_model(request, infer)

    async def request_error(request: Request, exc: _RequestError):
        return _error(exc.status, exc.category)

    async def http_error(request: Request, exc: HTTPException):
        return _error(exc.status_code, "http_error")

    app = Starlette(routes=[Route("/health/ready", ready, methods=["GET"]),
                            Route("/v1/embeddings", embeddings, methods=["POST"]),
                            Route("/v1/rerank", rerank, methods=["POST"])],
                    lifespan=lifespan, exception_handlers={_RequestError: request_error, HTTPException: http_error})
    app.state.ready = False
    app.state.warmup_status = "not_started"
    app.state.inference_lock = asyncio.Lock()
    app.add_middleware(_Boundary)
    return app


def build_app(config: ModelServerConfig) -> Starlette:
    token = read_token(config.token_file) if config.token_file is not None else None
    root = Path(config.model_root).expanduser().resolve()
    options = {"device": config.device, "dtype": config.dtype, "batch_size": config.batch_size,
               "max_length": config.max_length, "timeout": config.timeout}
    embedder = Qwen3Embedder(root / "Qwen3-Embedding-0.6B", model_id=EMBEDDING_MODEL,
                            dimensions=config.dimensions, model_revision=config.model_revision, **options)
    reranker = Qwen3Reranker(root / "Qwen3-Reranker-0.6B", model_id=RERANKING_MODEL, **options)
    return create_app(embedder, reranker, token=token)


def parse_args(argv: Sequence[str] | None = None) -> ModelServerConfig:
    parser = argparse.ArgumentParser(description="Bounded local Qwen knowledge-model server (no downloads)",
                                     allow_abbrev=False)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18792)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--dtype", default="float32", choices=["float32", "float16", "bfloat16"])
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--max-length", type=int, default=4096)
    parser.add_argument("--dimensions", type=int, default=1024)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--model-revision", default="")
    return ModelServerConfig(**vars(parser.parse_args(argv)))


def main(argv: Sequence[str] | None = None) -> None:
    try:
        config = parse_args(argv)
        app = build_app(config)
        configure_threads(config.threads)
    except ModelError:
        raise SystemExit("model server setup failed: invalid configuration, token file or local model files") from None
    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port, workers=1, access_log=False,
                proxy_headers=False, server_header=False, log_level="warning")


if __name__ == "__main__":
    main()
