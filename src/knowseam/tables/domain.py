from decimal import Decimal, InvalidOperation
import heapq
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..identifiers import Identifier


class TableModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)


class Column(TableModel):
    column_id: Identifier
    index: Annotated[int, Field(strict=True, ge=1, le=16384)]
    header_path: Annotated[tuple[Annotated[str, Field(strict=True, max_length=4096)], ...], Field(max_length=32)] = ()


class Cell(TableModel):
    row: Annotated[int, Field(strict=True, ge=1, le=1000000)]
    column: Annotated[int, Field(strict=True, ge=1, le=16384)]
    row_span: Annotated[int, Field(strict=True, ge=1, le=1000000)] = 1
    column_span: Annotated[int, Field(strict=True, ge=1, le=16384)] = 1
    raw_text: Annotated[str, Field(strict=True, max_length=65536)]
    value_kind: Literal['empty', 'text', 'number', 'boolean', 'date', 'error', 'unknown'] = 'unknown'
    value: Annotated[str, Field(strict=True, max_length=65536)] | None = None
    unit: Annotated[str, Field(strict=True, min_length=1, max_length=128)] | None = None
    formula: Annotated[str, Field(strict=True, max_length=8192)] | None = None
    formula_state: Literal['not_formula', 'cache_present', 'cache_missing', 'unknown'] = 'not_formula'

    @model_validator(mode='after')
    def values(self):
        if self.value_kind == 'number':
            try:
                if self.value is None or not Decimal(self.value).is_finite():
                    raise ValueError('number must be an exact finite decimal string')
            except InvalidOperation:
                raise ValueError('invalid decimal') from None
        if self.value_kind == 'empty' and self.value is not None:
            raise ValueError('empty cell has no value')
        if self.value_kind == 'boolean' and self.value not in ('true', 'false'):
            raise ValueError('invalid boolean cell')
        if self.formula_state in ('cache_present', 'cache_missing') and self.formula is None:
            raise ValueError('formula required')
        if self.formula_state == 'not_formula' and self.formula is not None:
            raise ValueError('inconsistent formula state')
        return self


class ParsedTable(TableModel):
    table_id: Identifier
    name: Annotated[str, Field(strict=True, max_length=4096)] = ''
    row_count: Annotated[int, Field(strict=True, ge=0, le=1000000)]
    columns: Annotated[tuple[Column, ...], Field(max_length=16384)]
    cells: Annotated[tuple[Cell, ...], Field(max_length=200000)]
    locator: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def coordinates(self):
        cols = {c.index for c in self.columns}
        if len(cols) != len(self.columns) or len({c.column_id for c in self.columns}) != len(cols):
            raise ValueError('duplicate column')
        columns_mask = sum(1 << c for c in cols)
        active, vertical_mask, occupied, row = [], 0, 0, -1
        for cell in sorted(self.cells, key=lambda c: (c.row, c.column)):
            if cell.row + cell.row_span - 1 > self.row_count:
                raise ValueError('duplicate or out-of-range cell')
            mask = ((1 << cell.column_span) - 1) << cell.column
            if mask & columns_mask != mask:
                raise ValueError('cell column outside table')
            if cell.row != row:
                while active and active[0][0] < cell.row:
                    _, removed = heapq.heappop(active)
                    vertical_mask ^= removed
                row, occupied = cell.row, vertical_mask
            if occupied & mask:
                raise ValueError('overlapping cell spans')
            occupied |= mask
            if cell.row_span > 1:
                vertical_mask |= mask
                heapq.heappush(active, (cell.row + cell.row_span - 1, mask))
        return self


def validate_tables(tables):
    parsed = tuple(ParsedTable.model_validate(t) for t in tables)
    if len(parsed) > 1000 or sum(len(t.cells) for t in parsed) > 200000:
        raise ValueError('table structure limit')
    if len({t.table_id for t in parsed}) != len(parsed):
        raise ValueError('duplicate table identity')
    return parsed
