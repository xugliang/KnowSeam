"""Bounded evidence labels shared by evaluation and source-backed capabilities."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
Index = Annotated[int, Field(strict=True, ge=0)]


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    document_id: Text
    version_id: Text
    section_id: Text | None = None
    start_char: Index | None = None
    end_char: Index | None = None
    table_id: Text | None = None
    row: Annotated[int, Field(strict=True, ge=1)] | None = None
    column_id: Text | None = None
    expected_value: Annotated[str, Field(strict=True, max_length=65536)] | None = None
    unit: Text | None = None

    @model_validator(mode="after")
    def anchor(self):
        text = self.section_id is not None and self.start_char is not None and self.end_char is not None
        cell = self.table_id is not None and self.row is not None and self.column_id is not None
        text_fields = (self.section_id, self.start_char, self.end_char)
        cell_fields = (self.table_id, self.row, self.column_id)
        if (text == cell or any(v is not None for v in text_fields) and not text
                or any(v is not None for v in cell_fields) and not cell
                or (text and self.end_char <= self.start_char)
                or (not cell and (self.expected_value is not None or self.unit is not None))):
            raise ValueError("evidence requires a nonempty text span or exact cell")
        return self


class EvidenceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    alternatives: Annotated[tuple[EvidenceSpan, ...], Field(min_length=1, max_length=32)]
