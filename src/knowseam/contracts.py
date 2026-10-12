"""Validated optional capability requests, independent of the old repository ABC."""
from abc import ABC, abstractmethod
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Identifier
from .domain import ParsedDocument, Principal


class RequestModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True, hide_input_in_errors=True)


class ContextRequest(RequestModel):
    document_id: Identifier
    version_id: Identifier
    chunk_ids: Annotated[list[Identifier], Field(min_length=1, max_length=20)]
    mode: Literal['neighbors', 'section'] = 'neighbors'
    before: Annotated[int, Field(ge=0, le=5)] = 1
    after: Annotated[int, Field(ge=0, le=5)] = 1
    max_chars: Annotated[int, Field(ge=1000, le=30000)] = 12000

    @field_validator('chunk_ids')
    @classmethod
    def unique_chunks(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('duplicate chunks')
        return value


class ContextReader(ABC):
    @abstractmethod
    def read_context(self, principal: Principal, request: ContextRequest) -> dict: ...


class StructurePublisher(ABC):
    @abstractmethod
    def publish_structured(self, parsed: ParsedDocument, **publication) -> str: ...
