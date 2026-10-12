"""Restart-safe synchronous publication driven by an async, read-only source connector."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import os
import logging
import tempfile
from typing import Any

from .chunking import CHUNKER_VERSION, canonical, chunk_document, digest, parsed_hash
from .domain import AccessDenied, DocumentParser, Embedder, SourceConnector, UnsupportedDocument
from .repository import KnowledgeRepository
from .contracts import StructurePublisher
from .permissions.domain import PermissionChanged

LOGGER = logging.getLogger("knowseam.sync")


class SyncWorker:
    def __init__(self, store: KnowledgeRepository, parser: DocumentParser, embedder: Embedder, tokenizer: Any,
                 file_root: Path, *, max_file_bytes: int = 32 * 1024 * 1024,
                 max_chunks: int = 10000, structure_publisher: StructurePublisher | None = None, indexing=None):
        self.store, self.parser, self.embedder, self.tokenizer = store, parser, embedder, tokenizer
        self.file_root = Path(file_root).resolve()
        self.max_file_bytes, self.max_chunks = max_file_bytes, max_chunks
        self.structure_publisher = structure_publisher
        self.indexing = indexing

    @property
    def pipeline_id(self) -> str:
        values = {"parser": getattr(self.parser, "version", "structured-v1"),
                                 "chunker": CHUNKER_VERSION, "model": self.embedding_identity,
                                 "dimensions": self.embedder.dimensions,
                                 "tokenizer": getattr(self.tokenizer, "version", "jieba-company-v1")}
        if self.structure_publisher is not None:
            values['structure'] = 'original-section-spans-v1'
            identity = getattr(self.structure_publisher, 'identity', None)
            if identity is not None:
                values['publication'] = identity
        if self.indexing is not None:
            values['indexing'] = self.indexing.identity
        return digest(canonical(values))

    @property
    def embedding_identity(self) -> str:
        return getattr(self.embedder, "cache_identity", self.embedder.model_id)

    def _archive(self, data: bytes) -> str:
        checksum = digest(data)
        target = self.file_root / "objects" / checksum[:2] / checksum
        target.parent.mkdir(parents=True, exist_ok=True)
        # Do not follow a pre-existing malicious symlink inside the managed data volume.
        if not target.parent.resolve().is_relative_to(self.file_root):
            raise ValueError("archive path escaped data root")
        if target.exists():
            if target.is_symlink() or digest(target.read_bytes()) != checksum:
                raise ValueError("archive integrity failure")
            return str(target.relative_to(self.file_root))
        fd, tmp = tempfile.mkstemp(prefix=".ingest-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return str(target.relative_to(self.file_root))

    async def run(self, source_id: str, connector: SourceConnector, *, force: bool = False,
                  config_hash: str | None = None) -> dict:
        counts = {"seen": 0, "published": 0, "unchanged": 0, "unavailable": 0, "failed": 0, "deleted": 0}
        # Advisory lock is held by a dedicated DB connection across async fetch/parse/index work.
        with self.store.source_lock(source_id, config_hash):
            authority = getattr(self.store, 'permissions', None)
            observation = await asyncio.to_thread(authority.begin, source_id) if authority is not None else None
            scanned_at = datetime.now(timezone.utc)
            try:
                snapshot = await connector.scan()
            except Exception:
                counts["failed"] += 1
                self.store.finish_scan(source_id, complete=False, counts=counts)
                raise
            seen: list[str] = []
            ids = set()
            for node in snapshot.nodes:
                if not node.external_id or node.external_id in ids:
                    # Ambiguous listing cannot be used to authorize or detect deletion.
                    counts["failed"] += 1
                    self.store.finish_scan(source_id, complete=False, counts=counts)
                    raise ValueError("source listing contains empty or duplicate document IDs")
                ids.add(node.external_id)
            # Refresh/revoke all observed ACLs before starting potentially slow embedding work.
            observed = []
            for node in snapshot.nodes:
                seen.append(node.external_id)
                counts["seen"] += 1
                current = self.store.observe(source_id, node, scanned_at, **(
                    {'_observation': observation} if observation is not None else {}))
                observed.append((node, current))
            if snapshot.complete:
                counts["deleted"] = self.store.reconcile(source_id, seen)
            for node, current in observed:
                permission_revision = None
                if authority is not None:
                    from .permissions.worker import PermissionWorker
                    await PermissionWorker(authority).refresh(source_id, connector, [node.external_id])
                    with self.store.connect() as connection:
                        permission_revision = authority.admission(connection, current['id'])
                if (authority is None and node.public is not True) or (authority is not None and permission_revision is None):
                    counts["unavailable"] += 1
                    continue
                permission_args = {'_permission_revision': permission_revision} if authority is not None else {}
                if (not force and node.revision and current["source_revision"] == node.revision
                        and current["active_version"] and current["pipeline_id"] == self.pipeline_id):
                    self.store.touch_unchanged(current["id"], node.revision, **permission_args)
                    counts["unchanged"] += 1
                    continue
                try:
                    stage = "fetch"
                    content = await connector.fetch(node)
                    if authority is not None:
                        await PermissionWorker(authority).refresh(source_id, connector, [node.external_id])
                        with self.store.connect() as connection:
                            permission_revision = authority.admission(connection, current['id'])
                        if permission_revision is None:
                            raise AccessDenied('source_permission_changed_during_fetch')
                        permission_args = {'_permission_revision': permission_revision}
                    if not content.data or len(content.data) > self.max_file_bytes:
                        raise ValueError("empty or oversized source content")
                    stage = "parse"
                    parsed = await asyncio.to_thread(self.parser.parse, content)
                    stage = "chunk"
                    chunks = chunk_document(parsed, source_spans=self.structure_publisher is not None)
                    visual_assets = parsed.assets if getattr(self.store,'visual_jobs',None) is not None else ()
                    if not chunks and not visual_assets:
                        raise UnsupportedDocument("no searchable content extracted")
                    if len(chunks) > self.max_chunks:
                        raise ValueError("document chunk limit exceeded")
                    content_hash = parsed_hash(parsed)
                    if (current["active_version"] and current["content_hash"] == content_hash
                            and current["pipeline_id"] == self.pipeline_id):
                        self.store.touch_unchanged(current["id"], node.revision, **permission_args)
                        counts["unchanged"] += 1
                        continue
                    stage = "embed"
                    cached = self.store.cached_embeddings(self.embedding_identity, self.embedder.dimensions,
                                                          [chunk.content_hash for chunk in chunks])
                    missing = {chunk.content_hash: chunk.embedding_text for chunk in chunks if chunk.content_hash not in cached}
                    if missing:
                        pairs = list(missing.items())
                        for batch_start in range(0, len(pairs), 32):
                            batch = pairs[batch_start:batch_start + 32]
                            vectors = await self.embedder.embed([item[1] for item in batch], query=False)
                            if len(vectors) != len(batch):
                                raise ValueError("embedding count mismatch")
                            cached.update(zip([item[0] for item in batch], vectors, strict=True))
                    stage = 'tokenize'
                    index_batch = (await asyncio.to_thread(self.indexing.prepare, source_id, chunks, self.tokenizer,
                                                          document_id=current['id'])
                                   if self.indexing is not None else None)
                    token_texts = ([' '.join(p.tokens) for p in index_batch.payloads] if index_batch else
                                   [self.tokenizer.fts_text(chunk.embedding_text) for chunk in chunks])
                    stage = "publish"
                    raw_path = await asyncio.to_thread(self._archive, content.data)
                    publication = dict(doc_id=current["id"], node=node, chunks=chunks,
                                       embeddings=[cached[chunk.content_hash] for chunk in chunks],
                                       token_texts=token_texts,
                                       model_id=self.embedding_identity, dimensions=self.embedder.dimensions,
                                       pipeline_id=self.pipeline_id, raw_hash=digest(content.data),
                                       content_hash=content_hash, raw_path=raw_path, warnings=parsed.warnings)
                    if index_batch is not None:
                        publication['_index_batch'] = index_batch
                    publication.update(permission_args)
                    if visual_assets:
                        publication['_visual_requests'] = visual_assets
                    if self.structure_publisher is None:
                        self.store.publish(**publication)
                    else:
                        self.structure_publisher.publish_structured(parsed, **publication)
                    counts["published"] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    LOGGER.warning("document_sync_failed document_id=%s stage=%s category=%s",
                                   current["id"], stage, type(exc).__name__)
                    # Store only the class, never tokens, source text or vendor exception bodies.
                    self.store.fail_document(current["id"], type(exc).__name__,
                                             revoke=isinstance(exc, AccessDenied) and not isinstance(exc, PermissionChanged))
            counts["failed"] += len(snapshot.errors)
            self.store.finish_scan(source_id, complete=snapshot.complete, counts=counts)
        return counts
