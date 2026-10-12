from abc import ABC, abstractmethod
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..identifiers import Identifier
from ..domain import Principal

Name = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
Kind = Annotated[str, Field(strict=True, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._:-]*$')]


class CatalogRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True, hide_input_in_errors=True)
    collection_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    kind: Kind
    cursor: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50

    @field_validator('collection_ids')
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('duplicate scope')
        return value


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    source_id: Identifier
    external_id: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
    kind: Kind
    item_id: Identifier
    name: Name
    aliases: Annotated[tuple[Name, ...], Field(max_length=32)] = ()
    metadata: dict[Annotated[str, Field(max_length=64)], Name] = Field(default_factory=dict)

    @field_validator('metadata')
    @classmethod
    def bounded_metadata(cls, value):
        if len(value) > 16:
            raise ValueError('catalog metadata limit')
        return value


class CatalogScope(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    source_id: Identifier
    kind: Kind


class CatalogConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    entries: Annotated[tuple[CatalogEntry, ...], Field(max_length=10000)] = ()
    scopes: Annotated[tuple[CatalogScope, ...], Field(max_length=1000)] = ()


class CatalogReader(ABC):
    @abstractmethod
    def list_catalog(self, principal: Principal, request: CatalogRequest) -> dict: ...
