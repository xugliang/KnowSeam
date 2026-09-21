"""Versioned connector/parser contracts; no dependency on SuiteHarness internals."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence


class KnowledgeError(Exception):
    pass


class AccessDenied(KnowledgeError):
    pass


class NotFound(KnowledgeError):
    pass


class UnsupportedDocument(KnowledgeError):
    pass


class IncompleteSnapshot(KnowledgeError):
    pass


@dataclass(frozen=True)
class Principal:
    """Created by trusted authentication, never taken from LLM tool arguments."""

    actor_id: str
    company_id: str
    product_id: str
    allowed_collections: tuple[str, ...]


@dataclass(frozen=True)
class SourceNode:
    external_id: str
    title: str
    revision: str
    media_type: str
    source_url: str
    # True only when company-wide publication has been positively verified.
    public: bool | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSnapshot:
    nodes: tuple[SourceNode, ...]
    complete: bool = True
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceContent:
    data: bytes
    filename: str
    media_type: str


@dataclass(frozen=True)
class Section:
    text: str
    locator: dict[str, Any] = field(default_factory=dict)
    heading: str = ""


@dataclass(frozen=True)
class ParsedDocument:
    sections: tuple[Section, ...]
    warnings: tuple[str, ...] = ()
    parser_version: str = "structured-v1"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    text: str
    embedding_text: str
    content_hash: str
    locator: dict[str, Any]
    ordinal: int
    heading: str = ""


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    version_id: str
    chunk_id: str
    collection_id: str
    title: str
    text: str
    source_url: str
    locator: dict[str, Any]
    score: float
    stale: bool = False


class SourceConnector(ABC):
    @abstractmethod
    async def scan(self) -> SourceSnapshot: ...

    @abstractmethod
    async def fetch(self, node: SourceNode) -> SourceContent: ...

    async def close(self) -> None:
        return None


class DocumentParser(ABC):
    @abstractmethod
    def parse(self, content: SourceContent) -> ParsedDocument: ...


class Embedder(ABC):
    model_id: str
    dimensions: int

    @abstractmethod
    async def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]: ...


class Reranker(ABC):
    model_id: str

    @abstractmethod
    async def score(self, query: str, documents: Sequence[str]) -> list[float]: ...

