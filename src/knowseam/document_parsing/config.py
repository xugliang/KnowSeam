from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Bool = Annotated[bool, Field(strict=True)]
Timeout = Annotated[int, Field(strict=True, ge=1, le=120)]
Pages = Annotated[int, Field(strict=True, ge=1, le=200)]
Sections = Annotated[int, Field(strict=True, ge=1, le=50000)]
FORMATS = frozenset({'pdf', 'png', 'jpg', 'jpeg', 'tif', 'tiff', 'docx', 'xlsx', 'pptx', 'txt', 'md'})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True, validate_default=True)


def explicit_path(value):
    if not isinstance(value, (str, Path)) or not str(value) or '\x00' in str(value):
        raise ValueError('explicit parser path required')
    path = PurePosixPath(str(value))
    if not path.is_absolute() or '..' in path.parts or '\\' in str(value) or str(value) == '/':
        raise ValueError('absolute dedicated parser path without traversal required')
    return Path(value)


class DoclingOptions(StrictModel):
    ocr_enabled: Bool = True
    table_structure_enabled: Bool = True
    timeout_seconds: Timeout = 120
    max_pages: Pages = 200
    max_sections: Sections = 50000


class DoclingConfig(DoclingOptions):
    enabled: Bool = False
    model_path: Path | None = None
    socket_path: Path = Path('/run/knowseam-parser/parser.sock')

    @field_validator('model_path', 'socket_path', mode='before')
    @classmethod
    def validate_path(cls, value):
        return None if value is None else explicit_path(value)

    def options(self):
        return DoclingOptions.model_validate(self.model_dump(include=set(DoclingOptions.model_fields)))


class ParsingConfig(StrictModel):
    docling: DoclingConfig = Field(default_factory=DoclingConfig)
    routes: dict[str, Literal['native', 'docling']] = Field(default_factory=dict)

    @field_validator('routes')
    @classmethod
    def supported_formats(cls, value):
        if not set(value) <= FORMATS:
            raise ValueError('unsupported parser routing format')
        return value
