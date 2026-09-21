"""Isolated knowledge-service operations; configuration comes only from JSON."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import date, datetime
import inspect
import json
import logging
import sys
import time
from typing import Any
import uuid

from .chunking import canonical, digest
from .config import FeishuSourceConfig, FilesystemSourceConfig, ModelConfig, Settings, load_config
from .storage import PostgresStore

LOGGER = logging.getLogger("knowseam.cli")
FULL_RECHECK_SECONDS = 24 * 60 * 60


def source_config_hash(source) -> str:
    # Credentials are not source semantics. Explicit exclusion is important:
    # SecretStr serialization would otherwise hash a constant mask.
    return digest(canonical(source.model_dump(mode="json", exclude={"app_secret"})))


def configure_store(store: PostgresStore, settings: Settings) -> None:
    store.configure(
        company_id=settings.company_id,
        collections=[item.model_dump(mode="json") for item in settings.collections],
        sources=[{"id": source.id, "collection_id": source.collection_id,
                  "config_hash": source_config_hash(source)} for source in settings.sources],
    )


def build_model(config: ModelConfig, *, embedding: bool):
    from .models import HTTPEmbedder, HTTPReranker, Qwen3Embedder, Qwen3Reranker
    common = {"model_id": config.model_id, "timeout": config.timeout}
    if embedding:
        common["dimensions"] = config.dimensions
        common["model_revision"] = config.model_revision
    if config.backend == "qwen":
        model_class = Qwen3Embedder if embedding else Qwen3Reranker
        return model_class(config.model_path, device=config.device, dtype=config.dtype,
                           max_length=config.max_length, batch_size=config.batch_size, **common)
    model_class = HTTPEmbedder if embedding else HTTPReranker
    return model_class(config.base_url, allow_online=config.allow_online,
                       api_key=config.api_key.get_secret_value(), **common)


def build_connector(source):
    if isinstance(source, FilesystemSourceConfig):
        from .connectors.filesystem import FilesystemConnector
        return FilesystemConnector(source.root, public_files=source.public_files,
                                   max_download_bytes=source.max_download_bytes)
    if isinstance(source, FeishuSourceConfig):
        from .connectors.feishu import FeishuWikiConnector
        return FeishuWikiConnector(**source.model_dump(exclude={"type", "id", "collection_id", "app_secret"}),
                                   app_secret=source.app_secret.get_secret_value())
    raise ValueError("unsupported configured source type")


async def _close_resource(resource: Any) -> None:
    method = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if method is not None:
        try:
            result = method()
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            LOGGER.error("resource_close_failed category=%s", type(error).__name__)


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError("unsupported output value")


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, default=_json_default))


async def sync_round(worker, sources, *, force: bool | dict[str, bool] = False) -> list[dict]:
    """Keep independent sources running; failure never advances full-scan state."""
    outcomes = []
    for source, connector in sources:
        try:
            forced = force.get(source.id, False) if isinstance(force, dict) else force
            counts = await worker.run(source.id, connector, force=forced, config_hash=source_config_hash(source))
            # A partial listing can return zero per-document failures. Check the
            # committed source status before claiming a successful complete scan.
            rows = await asyncio.to_thread(worker.store.status)
            complete = any(row["id"] == source.id and row["status"] == "ready" for row in rows)
            outcomes.append({"source_id": source.id, **counts, "complete": complete})
        except Exception as error:
            LOGGER.error("source_sync_failed category=%s", type(error).__name__)
            outcomes.append({"source_id": source.id, "failed": 1, "complete": False,
                             "error": type(error).__name__})
    return outcomes


async def run_worker(
    worker, sources, *, interval_seconds: int, sleep=asyncio.sleep, monotonic=time.monotonic,
) -> None:
    last_full_success: dict[str, float] = {}
    while True:
        started = monotonic()
        forced = {source.id: source.id not in last_full_success
                  or started - last_full_success[source.id] >= FULL_RECHECK_SECONDS for source, _ in sources}
        outcomes = await sync_round(worker, sources, force=forced)
        for outcome in outcomes:
            if forced[outcome["source_id"]] and outcome["complete"] and not outcome.get("failed"):
                # Use the beginning of the scan, not its completion, so long
                # scans do not postpone the next full-content verification.
                last_full_success[outcome["source_id"]] = started
        LOGGER.info("sync_cycle_complete sources=%d failed=%d", len(outcomes),
                    sum(not item["complete"] or bool(item.get("failed")) for item in outcomes))
        await sleep(max(0.1, interval_seconds - (monotonic() - started)))


async def execute(args: argparse.Namespace, settings: Settings) -> int:
    store = PostgresStore(settings.dsn.get_secret_value(), acl_ttl_seconds=settings.acl_ttl_seconds)
    async with AsyncExitStack() as resources:
        resources.push_async_callback(_close_resource, store)
        if args.command == "init-db":
            await asyncio.to_thread(store.initialize)
            await asyncio.to_thread(configure_store, store, settings)
            _print({"status": "initialized"})
            return 0
        if args.command == "status":
            ready = await asyncio.to_thread(store.ready)
            sources = await asyncio.to_thread(store.status) if ready else []
            _print({"database_ready": ready, "sources": sources})
            return 0 if ready else 1

        from .tokenization import JiebaTokenizer
        tokenizer = JiebaTokenizer(user_dictionary=settings.tokenization.user_dictionary,
                                   terms=settings.tokenization.terms)
        embedder = build_model(settings.models.embedding, embedding=True)
        resources.push_async_callback(_close_resource, embedder)

        if args.command in {"sync", "worker"}:
            from .parsers import StructuredDocumentParser
            from .sync import SyncWorker
            await asyncio.to_thread(configure_store, store, settings)
            # Actual non-sensitive embedding call, not a directory-exists probe.
            vectors = await embedder.embed(["企业知识库同步就绪检查"], query=False)
            if len(vectors) != 1 or len(vectors[0]) != embedder.dimensions:
                raise ValueError("embedding warmup shape mismatch")
            settings.file_root.mkdir(parents=True, exist_ok=True)
            sources = []
            for source in settings.sources:
                connector = build_connector(source)
                resources.push_async_callback(_close_resource, connector)
                sources.append((source, connector))
            max_bytes = max((source.max_download_bytes for source in settings.sources), default=30 * 1024 * 1024)
            worker = SyncWorker(store, StructuredDocumentParser(max_bytes=max_bytes), embedder, tokenizer,
                                settings.file_root, max_file_bytes=max_bytes)
            if args.command == "sync":
                outcomes = await sync_round(worker, sources, force=args.force)
                _print({"sources": outcomes})
                return 1 if any(not item["complete"] or item.get("failed") for item in outcomes) else 0
            await run_worker(worker, sources, interval_seconds=settings.sync_interval_seconds)
            return 0

        from .service import KnowledgeService
        reranker = build_model(settings.models.reranker, embedding=False)
        resources.push_async_callback(_close_resource, reranker)
        service = KnowledgeService(store, embedder, reranker, tokenizer)
        await service.warmup()
        if args.command == "check-models":
            _print({"models_ready": True, "embedding_model": embedder.model_id,
                    "embedding_dimensions": embedder.dimensions, "reranker_model": reranker.model_id})
            return 0
        if args.command == "serve":
            import uvicorn
            from .api import create_app
            await asyncio.to_thread(configure_store, store, settings)
            if not await service.ready():
                raise RuntimeError("knowledge service is not ready")
            app = create_app(service, settings.clients)
            server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, access_log=False,
                                                   log_level="warning"))
            await server.serve()
            return 0
        raise ValueError("unsupported command")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="独立企业知识库：所有业务配置从一个 JSON 文件读取")
    result.add_argument("--config", required=True, help="JSON 配置文件")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="初始化独立数据库及集合配置，不加载模型")
    commands.add_parser("status", help="只读检查数据库状态，不加载模型")
    commands.add_parser("check-models", help="执行非敏感真实模型推理，失败不返回就绪")
    sync = commands.add_parser("sync", help="执行一次增量同步")
    sync.add_argument("--force", action="store_true", help="重新获取全文校验，仍复用相同内容的嵌入缓存")
    commands.add_parser("worker", help="周期增量同步，启动时及每天完整复核")
    serve = commands.add_parser("serve", help="启动只读 API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=18790)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        if args.command == "serve" and not 1 <= args.port <= 65535:
            raise ValueError("invalid listening port")
        settings = load_config(args.config)
        return asyncio.run(execute(args, settings))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        error_id = uuid.uuid4().hex
        LOGGER.error("command_failed error_id=%s category=%s", error_id, type(error).__name__)
        print(json.dumps({"status": "failed", "error_id": error_id, "error": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
