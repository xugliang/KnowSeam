"""Opt-in diversity of authorized evidence, not a claim of universal completeness."""
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .retrieval import hit_key

FilterValue = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
FilterValues = Annotated[list[FilterValue], Field(min_length=1, max_length=32)]


class SearchOptionError(ValueError):
    pass


class SearchOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, hide_input_in_errors=True)
    strategy: Literal['legacy', 'coverage', 'relationship'] = 'legacy'
    filters: dict[Literal['kind', 'topic', 'document_type'], FilterValues] | None = None
    include_diagnostics: bool = False
    include_paths: bool = False

    @model_validator(mode='after')
    def path_strategy(self):
        if self.include_paths and self.strategy != 'relationship':
            raise ValueError('paths_require_relationship_strategy')
        return self

    @field_validator('filters')
    @classmethod
    def unique(cls, value):
        if value is not None and (not value or any(len(set(v)) != len(v) for v in value.values())):
            raise ValueError('invalid filter values')
        return value


class CoverageConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    max_per_document: Annotated[int, Field(strict=True, ge=1, le=20)] = 2
    allowed_filters: dict[Literal['kind', 'topic', 'document_type'], FilterValues] = Field(default_factory=dict)

    def validate_options(self, options):
        if options.filters and any(not set(values) <= set(self.allowed_filters.get(key, ()))
                                   for key, values in options.filters.items()):
            raise SearchOptionError('filter_not_registered')


@dataclass(frozen=True)
class CoverageSelection:
    hits: list
    coverage: dict


def _overlap(a, b):
    if (a.document_id, a.version_id, a.locator.get('section_id')) != (
            b.document_id, b.version_id, b.locator.get('section_id')) or not a.locator.get('section_id'):
        return False
    values = [h.locator.get(k) for h in (a, b) for k in ('char_start', 'char_end')]
    if not all(type(v) is int for v in values):
        return False
    x, y, u, v = values
    size = min(y - x, v - u)
    return size > 0 and max(0, min(y, v) - max(x, u)) / size >= 0.8


def select_coverage(hits, catalog_scope=None, *, max_per_document=2, limit=8):
    if type(limit) is not int or not 1 <= limit <= 20 or type(max_per_document) is not int or not 1 <= max_per_document <= 20:
        raise ValueError('invalid coverage limit')
    selected, counts, seen = [], {}, set()
    for hit in sorted(hits, key=lambda h: (-h.score, hit_key(h))):
        doc = (hit.collection_id, hit.document_id, hit.version_id)
        if hit_key(hit) in seen or counts.get(doc, 0) >= max_per_document or any(_overlap(hit, old) for old in selected):
            continue
        selected.append(hit)
        seen.add(hit_key(hit))
        counts[doc] = counts.get(doc, 0) + 1
        if len(selected) == limit:
            break
    coverage = {'returned_documents': len(counts), 'complete': None}
    if catalog_scope is not None:
        items = catalog_scope['items']
        represented = sum(any((item['collection_id'], e['document_id'], e['version_id']) in counts
                              for e in item['evidence']) for item in items)
        coverage.update(known_items=len(items), represented_items=represented,
                        complete=bool(items) and represented == len(items) if catalog_scope['complete'] else None)
    return CoverageSelection(selected, coverage)
