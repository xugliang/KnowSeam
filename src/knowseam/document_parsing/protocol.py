"""Versioned, bounded JSON protocol; never deserialize pickle or arbitrary objects."""
import base64
from dataclasses import asdict
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import Field, field_validator

from ..domain import ParsedDocument, Section, UnsupportedDocument
from .config import DoclingOptions, StrictModel

Hash = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]
HEADER = 'X-KnowSeam-Parse-Metadata'
MAX_BYTES = 30 * 1024 * 1024
MAX_RESPONSE_BYTES = 150 * 1024 * 1024


class ParserIdentity(StrictModel):
    protocol_version: Annotated[int, Field(strict=True, ge=1, le=2)] = 1
    upstream_sha: Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{40}$')]
    runtime_source_sha256: Hash
    dependency_fingerprint: Hash
    model_fingerprint: Hash
    normalizer_version: str = 'docling-structure-v1'


class ParseMetadata(StrictModel):
    filename: Annotated[str, Field(strict=True, min_length=1, max_length=255)]
    media_type: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    options: DoclingOptions = Field(default_factory=DoclingOptions)

    @field_validator('filename')
    @classmethod
    def leaf_filename(cls, value):
        if (PurePosixPath(value).name != value or PureWindowsPath(value).name != value
                or value in {'.', '..'} or ':' in value or '\\' in value
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError('single leaf filename required; paths and URLs forbidden')
        return value

    @field_validator('media_type')
    @classmethod
    def plain_type(cls, value):
        if any(ord(char) < 33 or ord(char) > 126 for char in value) or '/' not in value:
            raise ValueError('invalid media type')
        return value


class ParseMetadataV2(ParseMetadata):
    protocol_version: Literal[2] = 2


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def load_json(data):
    def reject(value):
        raise ValueError('non-finite number')
    return json.loads(data, object_pairs_hook=unique_object, parse_constant=reject)


def encode_metadata(metadata: ParseMetadata) -> str:
    value = base64.b64encode(metadata.model_dump_json().encode()).decode('ascii')
    if len(value) > 8192:
        raise UnsupportedDocument('parser_metadata_limit')
    return value


def decode_metadata(header: str, *, max_bytes=8192, allow_v2=False) -> ParseMetadata:
    if len(header) > max_bytes:
        raise ValueError('metadata limit')
    value = load_json(base64.b64decode(header, validate=True).decode('utf-8'))
    model = ParseMetadataV2 if allow_v2 and 'protocol_version' in value else ParseMetadata
    return model.model_validate(value)


def encode_document(document: ParsedDocument, *, protocol_version=1) -> bytes:
    if protocol_version not in (1, 2):
        raise UnsupportedDocument('parser_protocol_unsupported')
    value = {'sections': [asdict(s) for s in document.sections], 'warnings': document.warnings,
             'parser_version': document.parser_version}
    if protocol_version == 2:
        value.update(tables=[t.model_dump(mode='json') for t in document.tables], assets=list(document.assets))
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def _locator(value, depth=0):
    if depth > 10:
        raise ValueError('locator depth')
    if value is None or type(value) in (str, bool, int):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, (list, dict)) and len(value) <= 1000:
        for item in (value.values() if isinstance(value, dict) else value):
            _locator(item, depth + 1)
        return
    raise ValueError('invalid locator')


def decode_document(body: bytes, *, max_sections=50000, max_text_chars=MAX_RESPONSE_BYTES, protocol_version=1, allow_assets=False) -> ParsedDocument:
    try:
        values = load_json(body)
        if protocol_version not in (1, 2) or len(body) > MAX_RESPONSE_BYTES:
            raise ValueError('protocol or response limit')
        expected = {'sections', 'warnings', 'parser_version'} | ({'tables', 'assets'} if protocol_version == 2 else set())
        if set(values) != expected:
            raise ValueError('unexpected response fields')
        minimum = 0 if protocol_version==2 and allow_assets and values.get('assets') else 1
        if not isinstance(values['sections'], list) or not minimum <= len(values['sections']) <= max_sections:
            raise ValueError('empty or oversized sections')
        sections = []
        total = 0
        for section in values['sections']:
            if set(section) != {'text', 'locator', 'heading'} or not isinstance(section['locator'], dict):
                raise ValueError('invalid section')
            if not isinstance(section['text'], str) or not section['text'].strip() or not isinstance(section['heading'], str):
                raise ValueError('invalid text')
            total += len(section['text']) + len(section['heading'])
            if total > max_text_chars:
                raise ValueError('text limit')
            _locator(section['locator'])
            sections.append(Section(**section))
        if (not isinstance(values['warnings'], list) or len(values['warnings']) > 1000
                or any(not isinstance(w, str) or len(w) > 4096 for w in values['warnings'])
                or not isinstance(values['parser_version'], str) or not 1 <= len(values['parser_version']) <= 512):
            raise ValueError('invalid response metadata')
        tables, assets = (), ()
        if protocol_version == 2:
            from ..tables.domain import validate_tables
            if not isinstance(values['tables'], list) or not isinstance(values['assets'], list):
                raise ValueError('invalid structure arrays')
            tables = validate_tables(values['tables'])
            # Asset manifests get a separate authenticated transfer contract; paths are never accepted.
            if values['assets']:
                if not allow_assets or len(values['assets'])>500:
                    raise ValueError('asset capability not negotiated')
                from ..visual.domain import AssetRequest
                assets = tuple(AssetRequest.model_validate(a).model_dump(mode='json') for a in values['assets'])
            for table in tables:
                _locator(table.locator)
        return ParsedDocument(tuple(sections), tuple(values['warnings']), values['parser_version'], tables, assets, protocol_version)
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise UnsupportedDocument('parser_invalid_output') from None
