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
from dataclasses import asdict

from .chunking import canonical, digest
from .config import FeishuSourceConfig, FilesystemSourceConfig, ModelConfig, Settings, load_config
from .storage import PostgresStore
from .repository import KnowledgeRepository

LOGGER = logging.getLogger("knowseam.cli")
FULL_RECHECK_SECONDS = 24 * 60 * 60


def source_config_hash(source) -> str:
    # Credentials are not source semantics. Explicit exclusion is important:
    # SecretStr serialization would otherwise hash a constant mask.
    values = source.model_dump(mode="json", exclude={"app_secret"})
    # Newly optional empty exclusions must not invalidate every existing source.
    for key in ("excluded_node_tokens", "excluded_object_ids"):
        if not values.get(key):
            values.pop(key, None)
    if values.get("permission_mode") == "public_verified":
        values.pop("permission_mode", None)
    if not values.get("bindings"):
        values.pop("bindings", None)
    return digest(canonical(values))


def configure_store(store: KnowledgeRepository, settings: Settings) -> None:
    store.configure(
        company_id=settings.company_id,
        collections=[item.model_dump(mode="json") for item in settings.collections],
        employees=[item.model_dump(mode="json") for item in settings.employees],
        sources=[
            {
                "id": source.id,
                "collection_id": source.collection_id,
                "config_hash": source_config_hash(source),
                **(
                    {
                        "permission_mode": source.permission_mode,
                        "bindings": [b.model_dump() for b in getattr(source, "bindings", ())],
                    }
                    if settings.permissions.enabled
                    else {}
                ),
            }
            for source in settings.sources
        ],
    )
    if settings.features.catalog:
        from .catalog.postgres import configure_catalog

        configure_catalog(store, settings.company_id, settings.catalog)


def build_model(config: ModelConfig, *, embedding: bool):
    from .models import HTTPEmbedder, HTTPReranker, Qwen3Embedder, Qwen3Reranker

    common = {"model_id": config.model_id, "timeout": config.timeout}
    if embedding:
        common["dimensions"] = config.dimensions
        common["model_revision"] = config.model_revision
    if config.backend == "qwen":
        model_class = Qwen3Embedder if embedding else Qwen3Reranker
        return model_class(
            config.model_path,
            device=config.device,
            dtype=config.dtype,
            max_length=config.max_length,
            batch_size=config.batch_size,
            **common,
        )
    model_class = HTTPEmbedder if embedding else HTTPReranker
    return model_class(
        config.base_url, allow_online=config.allow_online, api_key=config.api_key.get_secret_value(), **common
    )


def build_connector(source):
    if isinstance(source, FilesystemSourceConfig):
        from .connectors.filesystem import FilesystemConnector

        return FilesystemConnector(
            source.root, public_files=source.public_files, max_download_bytes=source.max_download_bytes
        )
    if isinstance(source, FeishuSourceConfig):
        from .connectors.feishu import FeishuWikiConnector

        return FeishuWikiConnector(
            **source.model_dump(
                exclude={"type", "id", "collection_id", "app_secret", "permission_mode", "bindings"}
            ),
            app_secret=source.app_secret.get_secret_value(),
        )
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
            counts = await worker.run(
                source.id, connector, force=forced, config_hash=source_config_hash(source)
            )
            # A partial listing can return zero per-document failures. Check the
            # committed source status before claiming a successful complete scan.
            rows = await asyncio.to_thread(worker.store.status)
            complete = any(row["id"] == source.id and row["status"] == "ready" for row in rows)
            outcomes.append({"source_id": source.id, **counts, "complete": complete})
        except Exception as error:
            LOGGER.error("source_sync_failed category=%s", type(error).__name__)
            outcomes.append(
                {"source_id": source.id, "failed": 1, "complete": False, "error": type(error).__name__}
            )
    return outcomes


async def run_worker(
    worker,
    sources,
    *,
    interval_seconds: int,
    sleep=asyncio.sleep,
    monotonic=time.monotonic,
) -> None:
    last_full_success: dict[str, float] = {}
    while True:
        started = monotonic()
        forced = {
            source.id: source.id not in last_full_success
            or started - last_full_success[source.id] >= FULL_RECHECK_SECONDS
            for source, _ in sources
        }
        outcomes = await sync_round(worker, sources, force=forced)
        for outcome in outcomes:
            if forced[outcome["source_id"]] and outcome["complete"] and not outcome.get("failed"):
                # Use the beginning of the scan, not its completion, so long
                # scans do not postpone the next full-content verification.
                last_full_success[outcome["source_id"]] = started
        LOGGER.info(
            "sync_cycle_complete sources=%d failed=%d",
            len(outcomes),
            sum(not item["complete"] or bool(item.get("failed")) for item in outcomes),
        )
        await sleep(max(0.1, interval_seconds - (monotonic() - started)))


async def execute(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "visual-runtime":
        if not settings.visual.enabled:
            raise ValueError("visual_disabled")
        from .visual.runtime import serve_runtime

        await serve_runtime(settings.visual)
        return 0
    projections = (
        {collection: settings.graph.projection_key(collection) for collection in settings.graph.collections}
        if settings.graph.enabled or settings.features.history or settings.features.query_graph
        else {}
    )
    store = PostgresStore(
        settings.dsn.get_secret_value(),
        acl_ttl_seconds=settings.acl_ttl_seconds,
        graph_projections=projections,
        indexing_enabled=settings.indexing.enabled,
        history_enabled=settings.features.history or settings.features.query_graph,
    )
    store.fact_ontologies = settings.graph.collections
    store.fact_graph_config = settings.graph
    async with AsyncExitStack() as resources:
        resources.push_async_callback(_close_resource, store)
        if args.command == "index-rollback-legacy":
            from .indexing.postgres import materialize_legacy

            count = await asyncio.to_thread(materialize_legacy, store, settings.company_id)
            _print({"legacy_chunks_materialized": count})
            return 0
        if args.command == "migrate":
            from .migrations.runner import MigrationRunner

            runner = MigrationRunner(store.connect)
            rows = await asyncio.to_thread(runner.apply if args.apply else runner.plan)
            _print({"migrations": [asdict(row) for row in rows], "applied": args.apply})
            return 0
        if args.command == "init-db":
            await asyncio.to_thread(store.initialize)
            await asyncio.to_thread(configure_store, store, settings)
            _print({"status": "initialized"})
            return 0
        if settings.permissions.enabled:
            from .permissions.postgres import PermissionAuthority

            store.permissions = await asyncio.to_thread(PermissionAuthority, store)
        if settings.visual.enabled:
            from .visual.postgres import PostgresVisual
            from .visual.encoder import VisualClient
            from .visual.service import VisualService

            visual_encoder = VisualClient(settings.visual)
            resources.push_async_callback(_close_resource, visual_encoder)
            store.visual_jobs = PostgresVisual(store, settings.visual, settings.file_root)
            store.visual_service = VisualService(store.visual_jobs, visual_encoder)
        if args.command in {"visual-worker", "visual-rebuild"}:
            if not settings.visual.enabled:
                raise ValueError("visual_disabled")
            from .visual.worker import VisualWorker

            worker = VisualWorker(store.visual_jobs, visual_encoder, settings.company_id)
            cursor, next_scan, checked = None, 0.0, 0
            import time

            while True:
                if args.command == "visual-rebuild" or cursor is not None or time.monotonic() >= next_scan:
                    scan = await asyncio.to_thread(
                        store.visual_jobs.ensure,
                        settings.company_id,
                        retry_failed=args.command == "visual-rebuild",
                        after=cursor,
                    )
                    cursor = scan["next_cursor"]
                    checked += scan["checked"]
                    if cursor is None:
                        next_scan = time.monotonic() + 60
                        if args.command == "visual-rebuild":
                            _print({"visual_documents_checked": checked})
                            return 0
                    if args.command == "visual-rebuild":
                        continue
                result = await worker.step()
                _print({"visual_job": result})
                if args.once:
                    return 0
                if result["status"] == "idle":
                    await asyncio.sleep(30)
        if args.command in {"permissions-sync", "permissions-worker"}:
            if store.permissions is None:
                raise ValueError("permission worker requires permissions.enabled")
            from .permissions.worker import PermissionWorker

            worker = PermissionWorker(store.permissions)
            sources = []
            for source in settings.sources:
                connector = build_connector(source)
                resources.push_async_callback(_close_resource, connector)
                sources.append((source, connector))
            while True:
                results = []
                for source, connector in sources:
                    results.append(await worker.sync(source.id, connector, source_config_hash(source)))
                _print({"permission_sources": results})
                if args.command == "permissions-sync":
                    return 0
                await asyncio.sleep(settings.permissions.interval_seconds)
        if args.command == "status":
            ready = await asyncio.to_thread(store.ready)
            sources = await asyncio.to_thread(store.status) if ready else []
            _print({"database_ready": ready, "sources": sources})
            return 0 if ready else 1
        if args.command in {
            "facts-worker",
            "facts-rebuild",
            "graph-rebuild",
            "graph-status",
            "init-graph",
            "graph-worker",
        }:
            facts_only = args.command.startswith("facts-")
            if facts_only and not (
                store.history_enabled and settings.graph.collections and settings.graph.extractor
            ):
                raise ValueError("facts require history/query_graph, ontology and configured extractor")
            if not facts_only and not settings.graph.enabled:
                raise ValueError("graph command requires graph.enabled=true")
            if args.command == "graph-status":
                _print({"graph_jobs": await asyncio.to_thread(store.graph_status, settings.company_id)})
                return 0
            if args.command in {"facts-rebuild", "graph-rebuild"}:
                count = await asyncio.to_thread(store.graph_rebuild, settings.company_id)
                _print({"graph_documents_queued": count})
                return 0
            from .graph.factory import build_graph_store, build_graph_extractor

            graph_store = None
            if not facts_only:
                graph_store = build_graph_store(settings.graph)
                resources.push_async_callback(_close_resource, graph_store)
                await asyncio.to_thread(graph_store.initialize)
                if not await asyncio.to_thread(graph_store.ready):
                    raise RuntimeError("graph database is not ready")
            if args.command == "init-graph":
                _print({"graph_initialized": True})
                return 0
            from .graph.worker import GraphWorker

            await asyncio.to_thread(store.graph_ensure, settings.company_id)
            extractor = (
                build_graph_extractor(settings.graph, for_facts=True)
                if facts_only
                else build_graph_extractor(settings.graph)
            )
            resources.push_async_callback(_close_resource, extractor)
            extraction_cache = None
            if settings.graph.extraction_cache:
                from .indexing.dependencies import PostgresExtractionCache

                extraction_cache = await asyncio.to_thread(PostgresExtractionCache, store)
            fact_recorder = None
            if store.history_enabled:
                from .graph.facts import FactRecorder

                fact_recorder = FactRecorder(store)
            worker = GraphWorker(
                store,
                graph_store,
                extractor,
                settings.graph,
                settings.company_id,
                extraction_cache=extraction_cache,
                fact_recorder=fact_recorder,
            )
            if args.once:
                _print({"graph_job": await worker.step()})
            else:
                await worker.run()
            return 0

        from .tokenization import JiebaTokenizer

        tokenizer = JiebaTokenizer(
            user_dictionary=settings.tokenization.user_dictionary,
            terms=settings.tokenization.terms,
            glossary=[(item.canonical, item.aliases) for item in settings.tokenization.glossary],
        )
        if args.command == "migrate-bm25":
            await asyncio.to_thread(store.initialize)
            count = await asyncio.to_thread(store.backfill_bm25, tokenizer, company_id=settings.company_id)
            _print({"bm25_chunks_migrated": count})
            return 0
        embedder = build_model(settings.models.embedding, embedding=True)
        resources.push_async_callback(_close_resource, embedder)

        if args.command in {"sync", "worker"}:
            from .document_parsing.factory import build_parser
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
            max_bytes = max(
                (source.max_download_bytes for source in settings.sources), default=30 * 1024 * 1024
            )
            parser_client = None
            if settings.parsing.docling.enabled:
                from .document_parsing.client import ParserClient

                parser_client = ParserClient(
                    settings.parsing.docling.socket_path,
                    timeout_seconds=settings.parsing.docling.timeout_seconds,
                    max_bytes=max_bytes,
                )
                resources.push_async_callback(_close_resource, parser_client)
            document_parser = await asyncio.to_thread(
                build_parser,
                settings.parsing,
                max_bytes=max_bytes,
                client=parser_client,
                structured_tables=settings.features.tables,
            )
            if settings.visual.enabled:
                from .visual.assets import VisualParser

                document_parser = VisualParser(document_parser)
            from .publication import build_publisher

            structure_publisher = await asyncio.to_thread(
                build_publisher, store, settings.features, settings.catalog
            )
            indexing = None
            if settings.indexing.enabled:
                from .indexing.postgres import PostgresTokenCache

                indexing = await asyncio.to_thread(PostgresTokenCache, store, mode=settings.indexing.mode)
            worker = SyncWorker(
                store,
                document_parser,
                embedder,
                tokenizer,
                settings.file_root,
                max_file_bytes=max_bytes,
                structure_publisher=structure_publisher,
                indexing=indexing,
            )
            if args.command == "sync":
                outcomes = await sync_round(worker, sources, force=args.force)
                _print({"sources": outcomes})
                return 1 if any(not item["complete"] or item.get("failed") for item in outcomes) else 0
            await run_worker(worker, sources, interval_seconds=settings.sync_interval_seconds)
            return 0

        from .service import KnowledgeService

        reranker = build_model(settings.models.reranker, embedding=False)
        resources.push_async_callback(_close_resource, reranker)
        graph_search = None
        if args.command == "serve" and settings.graph.enabled:
            from .graph.factory import build_graph_store
            from .graph.search import GraphSearch

            graph_store = build_graph_store(settings.graph)
            resources.push_async_callback(_close_resource, graph_store)
            graph_search = GraphSearch(store, graph_store, settings.graph)
            store.query_projection = graph_store
        from .observability import LoggingObserver
        from .capabilities import build_capabilities

        service = KnowledgeService(
            store,
            embedder,
            reranker,
            tokenizer,
            graph_search=graph_search,
            capabilities=await asyncio.to_thread(build_capabilities, store, settings.features),
            coverage_config=settings.coverage,
            coverage_enabled=settings.features.coverage,
            diagnostic_actor_ids=settings.observability.diagnostic_actor_ids,
            observer=LoggingObserver() if settings.observability.enabled else None,
        )
        await service.warmup()
        if args.command == "evaluate":
            from .evaluation.runner import load_bundle, run_bundle
            from .eval import save_report

            bundle = load_bundle(args.manifest)
            if bundle.manifest.company_id != settings.company_id or not set(
                bundle.manifest.collection_ids
            ) <= {c.id for c in settings.collections}:
                raise ValueError("evaluation manifest configuration mismatch")
            report = await run_bundle(service, bundle)
            save_report(report, args.output)
            _print(
                {
                    "report_written": True,
                    "quality_verified": False,
                    "cases": len(bundle.cases),
                    "manifest_checksum": bundle.checksum,
                }
            )
            return 0
        if args.command == "check-models":
            _print(
                {
                    "models_ready": True,
                    "embedding_model": embedder.model_id,
                    "embedding_dimensions": embedder.dimensions,
                    "reranker_model": reranker.model_id,
                }
            )
            return 0
        if args.command == "serve":
            import uvicorn
            from .api import create_app

            await asyncio.to_thread(configure_store, store, settings)
            if not await service.ready():
                raise RuntimeError("knowledge service is not ready")
            app = create_app(service, settings.clients)
            server = uvicorn.Server(
                uvicorn.Config(app, host=args.host, port=args.port, access_log=False, log_level="warning")
            )
            await server.serve()
            return 0
        raise ValueError("unsupported command")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="独立企业知识库：所有业务配置从一个 JSON 文件读取")
    result.add_argument("--config", required=True, help="JSON 配置文件")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="初始化独立数据库及集合配置，不加载模型")
    commands.add_parser("status", help="只读检查数据库状态，不加载模型")
    migrate = commands.add_parser("migrate", help="显式检查或应用新增迁移，不加载模型")
    migration_mode = migrate.add_mutually_exclusive_group(required=True)
    migration_mode.add_argument("--dry-run", action="store_true")
    migration_mode.add_argument("--apply", action="store_true")
    commands.add_parser("init-graph", help="显式初始化可选 Neo4j 索引，不调用模型")
    commands.add_parser("graph-status", help="查看持久图谱任务状态，不调用模型")
    commands.add_parser("graph-rebuild", help="从当前数据库分块重建图谱，不下载或重新嵌入")
    graph_worker = commands.add_parser("graph-worker", help="消费持久图谱任务，抽取并更新证据投影")
    graph_worker.add_argument("--once", action="store_true", help="最多处理一个图谱任务并返回状态")
    facts_worker = commands.add_parser(
        "facts-worker", help="仅抽取PostgreSQL事实；不连接Neo4j，与graph-worker二选一消费同一队列"
    )
    facts_worker.add_argument("--once", action="store_true")
    commands.add_parser("facts-rebuild", help="为当前原文排队构建事实，记录当前接受时间而非倒填历史")
    commands.add_parser("visual-runtime", help="独立本地多模态编码服务，仅监听受限Unix socket")
    commands.add_parser("visual-rebuild", help="为当前视觉原文补齐新指纹/权限代际的任务，不回填虚构历史")
    visual_worker = commands.add_parser("visual-worker", help="独立消费视觉资产和编码任务")
    visual_worker.add_argument("--once", action="store_true")
    commands.add_parser("migrate-bm25", help="为现有活动分块补建 BM25 倒排索引，不调用模型")
    commands.add_parser("index-rollback-legacy", help="显式回填旧词项索引，停用复用前执行；不加载模型")
    commands.add_parser("permissions-sync", help="只同步已知文档权限，不读取正文或加载模型")
    commands.add_parser("permissions-worker", help="周期同步来源权限，与正文 worker 独立运行")
    commands.add_parser("check-models", help="执行非敏感真实模型推理，失败不返回就绪")
    evaluation = commands.add_parser("evaluate", help="按本地固定身份/索引/模型清单进行只读评测，不导出原文")
    evaluation.add_argument("--manifest", required=True)
    evaluation.add_argument("--output", required=True, help="新建去敏 JSON 报告；禁止覆盖已有文件")
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
        print(
            json.dumps({"status": "failed", "error_id": error_id, "error": type(error).__name__}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
