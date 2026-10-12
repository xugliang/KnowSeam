from abc import ABC, abstractmethod
from typing import Annotated
from pydantic import Field, field_validator

from ..contracts import RequestModel
from ..identifiers import Identifier


class TableListRequest(RequestModel):
    document_id: Identifier
    version_id: Identifier
    offset: Annotated[int, Field(ge=0, le=1000000)] = 0
    limit: Annotated[int, Field(ge=1, le=100)] = 50


class TableReadBody(RequestModel):
    version_id: Identifier
    row_start: Annotated[int, Field(ge=1, le=1000000)] = 1
    row_limit: Annotated[int, Field(ge=1, le=100)] = 50
    column_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)] | None = None

    @field_validator('column_ids')
    @classmethod
    def unique(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError('duplicate columns')
        return value


class TableReadRequest(TableReadBody):
    document_id: Identifier
    table_id: Identifier


class TableReader(ABC):
    @abstractmethod
    def list_tables(self, principal, request: TableListRequest) -> dict: ...

    @abstractmethod
    def read_table(self, principal, request: TableReadRequest) -> dict: ...
