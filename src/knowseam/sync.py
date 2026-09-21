"""Restart-safe synchronous publication driven by an async, read-only source connector."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import os
import tempfile
from typing import Any

from .chunking import CHUNKER_VERSION, canonical, chunk_document, digest, parsed_hash
from .domain import AccessDenied, DocumentParser, Embedder, SourceConnector, UnsupportedDocument
from .storage import PostgresStore


class SyncWorker:
    def __init__(self, store: PostgresStore, parser: DocumentParser, embedder: Embedder, tokenizer: Any,
                 file_root: Path, *, max_file_bytes: int = 32 * 1024 * 1024,
                 max_chunks: int = 10000):
        self.store, self.parser, self.embedder, self.tokenizer = store, parser, embedder, tokenizer
        self.file_root = Path(file_root).resolve()
        self.max_file_bytes, self.max_chunks = max_file_bytes, max_chunks

    @property
    def pipeline_id(self) -> str:
        return digest(canonical({"parser": getattr(self.parser, "version", "structured-v1"),
                                 "chunker": CHUNKER_VERSION, "model": self.embedding_identity,
                                 "dimensions": self.embedder.dimensions,
                                 "tokenizer": getattr(self.tokenizer, "version", "jieba-company-v1")}))

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
                current = self.store.observe(source_id, node, scanned_at)
                observed.append((node, current))
            if snapshot.complete:
                counts["deleted"] = self.store.reconcile(source_id, seen)
            for node, current in observed:
                if node.public is not True:
                    counts["unavailable"] += 1
                    continue
                if (not force and node.revision and current["source_revision"] == node.revision
                        and current["active_version"] and current["pipeline_id"] == self.pipeline_id):
                    self.store.touch_unchanged(current["id"], node.revision)
                    counts["unchanged"] += 1
                    continue
                try:
                    content = await connector.fetch(node)
                    if not content.data or len(content.data) > self.max_file_bytes:
                        raise ValueError("empty or oversized source content")
                    parsed = await asyncio.to_thread(self.parser.parse, content)
                    chunks = chunk_document(parsed)
                    if not chunks:
                        raise UnsupportedDocument("no searchable content extracted")
                    if len(chunks) > self.max_chunks:
                        raise ValueError("document chunk limit exceeded")
                    content_hash = parsed_hash(parsed)
                    if (current["active_version"] and current["content_hash"] == content_hash
                            and current["pipeline_id"] == self.pipeline_id):
                        self.store.touch_unchanged(current["id"], node.revision)
                        counts["unchanged"] += 1
                        continue
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
                    raw_path = await asyncio.to_thread(self._archive, content.data)
                    self.store.publish(doc_id=current["id"], node=node, chunks=chunks,
                                       embeddings=[cached[chunk.content_hash] for chunk in chunks],
                                       token_texts=[self.tokenizer.fts_text(chunk.embedding_text) for chunk in chunks],
                                       model_id=self.embedding_identity, dimensions=self.embedder.dimensions,
                                       pipeline_id=self.pipeline_id, raw_hash=digest(content.data),
                                       content_hash=content_hash, raw_path=raw_path, warnings=parsed.warnings)
                    counts["published"] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    # Store only the class, never tokens, source text or vendor exception bodies.
                    self.store.fail_document(current["id"], type(exc).__name__, revoke=isinstance(exc, AccessDenied))
            counts["failed"] += len(snapshot.errors)
            self.store.finish_scan(source_id, complete=snapshot.complete, counts=counts)
        return counts
