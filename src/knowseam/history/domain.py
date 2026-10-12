from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Annotated

from pydantic import Field, field_validator

from ..contracts import RequestModel
from ..identifiers import Identifier


class TemporalUnavailable(ValueError):
    """The requested past state cannot be established from retained evidence."""


def aware_time(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone_aware_timestamp_required')
    return value.astimezone(timezone.utc)


class TemporalQuery(RequestModel):
    valid_as_of: datetime
    known_as_of: datetime | None = None
    include_unknown_validity: bool = False

    _times = field_validator('valid_as_of', 'known_as_of', mode='before')(aware_time)


class HistorySearchRequest(TemporalQuery):
    query: Annotated[str, Field(min_length=1, max_length=4096, pattern=r'\S')]
    collection_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)] | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 8

    @field_validator('collection_ids')
    @classmethod
    def unique(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError('duplicate_collections')
        return value


class HistoryReadRequest(RequestModel):
    document_id: Identifier
    version_id: Identifier
    known_as_of: datetime | None = None
    offset: Annotated[int, Field(ge=0, le=1000000)] = 0
    limit: Annotated[int, Field(ge=1, le=100)] = 50

    _time = field_validator('known_as_of', mode='before')(aware_time)


class HistoryReader(ABC):
    @abstractmethod
    def search_history(self, principal, request: HistorySearchRequest) -> dict: ...

    @abstractmethod
    def read_history(self, principal, request: HistoryReadRequest) -> dict: ...
