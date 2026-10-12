"""Evidence-preserving graph contracts independent of any storage driver."""

from __future__ import annotations

import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
Name = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
Quote = Annotated[str, Field(strict=True, min_length=1, max_length=16000)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, validate_default=True)


def normalized_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _check_name(value: str) -> str:
    if not value.strip() or any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("graph names must not be blank or contain control characters")
    return value


class EvidenceRef(_Frozen):
    company_id: Identifier
    collection_id: Identifier
    document_id: Identifier
    version_id: Identifier
    chunk_id: Identifier


@dataclass(frozen=True)
class GraphChunk:
    evidence: EvidenceRef
    text: str
    locator: dict


class GraphOntology(_Frozen):
    entity_types: Annotated[tuple[Name, ...], Field(min_length=1, max_length=128)]
    relation_types: Annotated[tuple[Name, ...], Field(min_length=1, max_length=128)]
    aliases: dict[Name, Name] = Field(default_factory=dict, max_length=10000)
    version: Name = "1"
    single_valued_relations: tuple[Name, ...] = ()

    @model_validator(mode="after")
    def unambiguous_names(self):
        for values in (self.entity_types, self.relation_types):
            for value in values:
                _check_name(value)
            if len(set(values)) != len(values):
                raise ValueError("graph ontology types must be unique")
        seen: dict[str, str] = {}
        for alias, canonical in self.aliases.items():
            _check_name(alias)
            _check_name(canonical)
            normal = normalized_name(alias)
            target = normalized_name(canonical)
            if normal in seen and seen[normal] != target:
                raise ValueError("graph aliases must have unambiguous canonical names")
            seen[normal] = target
        for start in seen:
            visited = set()
            current = start
            while current in seen and seen[current] != current:
                if current in visited:
                    raise ValueError("graph aliases must not contain cycles")
                visited.add(current)
                current = seen[current]
        _check_name(self.version)
        if (not set(self.single_valued_relations)<=set(self.relation_types)
                or len(set(self.single_valued_relations))!=len(self.single_valued_relations)):
            raise ValueError('single_valued_relations_must_be_unique_ontology_subset')
        return self


class GraphEntity(_Frozen):
    key: Name
    name: Name
    type: Name
    quote: Quote
    aliases: Annotated[tuple[Name, ...], Field(max_length=32)] = ()
    qualifier: Annotated[str, Field(strict=True, max_length=256)] = ""

    @field_validator("key", "name", "type")
    @classmethod
    def named(cls, value):
        return _check_name(value)


class GraphRelation(_Frozen):
    subject: Name
    object: Name
    type: Name
    quote: Quote
    explicit: Annotated[bool, Field(strict=True)] = True
    confidence: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] = 1.0
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    valid_from_quote: Quote | None = None
    valid_until_quote: Quote | None = None

    @model_validator(mode="after")
    def valid_interval(self):
        for value in (self.valid_from, self.valid_until):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("graph validity timestamps must be timezone-aware")
        if self.valid_from is not None and self.valid_until is not None and self.valid_until < self.valid_from:
            raise ValueError("graph validity interval must be ordered")
        return self


class ChunkExtraction(_Frozen):
    entities: Annotated[tuple[GraphEntity, ...], Field(max_length=256)]
    relations: Annotated[tuple[GraphRelation, ...], Field(max_length=1024)]


@dataclass(frozen=True)
class GraphDocument:
    company_id: str
    collection_id: str
    document_id: str
    version_id: str
    event_sequence: int
    projection_key: str
    extractor_version: str
    model_id: str
    model_revision: str
    chunks: tuple[GraphChunk, ...]
    extractions: tuple[ChunkExtraction, ...] = ()


@dataclass(frozen=True)
class GraphEdge:
    evidence: EvidenceRef
    subject_id: str
    object_id: str
    relation_type: str
    quote: str
    explicit: bool
    confidence: float
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    observation_id: str = ''


class GraphStore(ABC):
    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def ready(self) -> bool: ...

    @abstractmethod
    def replace_document(self, document: GraphDocument) -> None: ...

    @abstractmethod
    def remove_document(self, company_id: str, collection_id: str, document_id: str,
                        event_sequence: int) -> None: ...

    @abstractmethod
    def seed_entities(self, company_id: str, collection_id: str,
                      evidence: Sequence[EvidenceRef], limit: int) -> list[str]: ...

    @abstractmethod
    def neighbors(self, company_id: str, collection_id: str,
                  entity_ids: Sequence[str], limit: int) -> list[GraphEdge]: ...

    def close(self) -> None:
        pass


class GraphExtractor(ABC):
    @abstractmethod
    async def extract(self, chunk: GraphChunk, ontology: GraphOntology) -> ChunkExtraction: ...

    async def close(self) -> None:
        pass
