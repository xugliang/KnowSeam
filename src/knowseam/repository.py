"""Storage seams: authority and retrieval are independent of an agent runtime.

An adapter must implement the core contract. Optional capabilities are composed
separately rather than adding unrelated abstract methods to every adapter.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Sequence

from .domain import AccessDenied, Chunk, Principal, SearchHit, SourceNode


class RetrievalBackend(ABC):
    @abstractmethod
    def retrieve(self, principal: Principal, requested: Sequence[str] | None, *,
                 vector: Sequence[float], model_id: str, dimensions: int,
                 tokens: Sequence[str], limit: int = 40) -> tuple[list[SearchHit], list[SearchHit]]: ...


class KnowledgeRepository(ABC):
    """Documents, publication, source state and authorization are authoritative here."""

    def select_collections(self, principal: Principal, requested: Sequence[str] | None) -> list[str]:
        if not principal.actor_id or not principal.company_id or not principal.product_id:
            raise AccessDenied("authenticated principal required")
        selected = list(principal.allowed_collections if requested is None else requested)
        if not set(selected).issubset(principal.allowed_collections):
            raise AccessDenied("collection not allowed")
        return list(dict.fromkeys(selected))

    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def ready(self) -> bool: ...

    @abstractmethod
    def configure(self, *, company_id: str, collections: Sequence[dict], sources: Sequence[dict],
                  employees: Sequence[dict] = ()) -> None: ...

    @abstractmethod
    def source_lock(self, source_id: str, expected_config_hash: str | None = None) -> AbstractContextManager: ...

    @abstractmethod
    def observe(self, source_id: str, node: SourceNode, scanned_at: datetime) -> dict: ...

    @abstractmethod
    def touch_unchanged(self, doc_id: str, revision: str) -> None: ...

    @abstractmethod
    def fail_document(self, doc_id: str, error_code: str, *, revoke: bool = False) -> None: ...

    @abstractmethod
    def reconcile(self, source_id: str, seen: Sequence[str]) -> int: ...

    @abstractmethod
    def finish_scan(self, source_id: str, *, complete: bool, counts: dict) -> None: ...

    @abstractmethod
    def cached_embeddings(self, model_id: str, dimensions: int,
                          hashes: Sequence[str]) -> dict[str, list[float]]: ...

    @abstractmethod
    def publish(self, *, doc_id: str, node: SourceNode, chunks: Sequence[Chunk],
                embeddings: Sequence[Sequence[float]], token_texts: Sequence[str], model_id: str,
                dimensions: int, pipeline_id: str, raw_hash: str, content_hash: str,
                raw_path: str, warnings: Sequence[str]) -> str: ...

    @abstractmethod
    def collections(self, principal: Principal) -> list[dict]: ...

    @abstractmethod
    def revalidate_hits(self, principal: Principal, hits: Sequence[SearchHit]) -> list[SearchHit]: ...

    @abstractmethod
    def read(self, principal: Principal, doc_id: str, version_id: str, offset: int, limit: int) -> dict: ...

    @abstractmethod
    def status(self) -> list[dict]: ...

    def close(self) -> None:
        """Default for adapters without a persistent connection or pool."""
