"""Normalize actual Docling items, without flattening Markdown or generating facts."""
from ..domain import ParsedDocument, Section, UnsupportedDocument
from .extensions.cleanup import clean_text
from .extensions.tables import table_sections, structured_table

import hashlib
from pathlib import Path

# Adapter/normalizer customizations invalidate the pipeline without relying on
# a maintainer remembering to bump a literal version or on .git in the image.
_ROOT = Path(__file__).parent
_FILES = ('normalizer.py', 'docling_adapter.py', 'extensions/tables.py', 'extensions/cleanup.py')
NORMALIZER_VERSION = 'docling-structure-v2-' + hashlib.sha256(
    b''.join(name.encode() + b'\0' + (_ROOT / name).read_bytes() for name in _FILES)
    + (_ROOT.parent / 'tables/domain.py').read_bytes()).hexdigest()


def office_context(document, item, locator):
    parent = getattr(item, 'parent', None)
    for _ in range(32):
        if parent is None or not callable(getattr(parent, 'resolve', None)):
            break
        group = parent.resolve(document)
        label = getattr(getattr(group, 'label', None), 'value', getattr(group, 'label', None))
        if label == 'sheet':
            locator['sheet'] = str(group.name)
            break
        parent = getattr(group, 'parent', None)
    media = getattr(getattr(document, 'origin', None), 'mimetype', '')
    if media in {'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                 'application/vnd.openxmlformats-officedocument.presentationml.template',
                 'application/vnd.openxmlformats-officedocument.presentationml.slideshow'} and 'page' in locator:
        locator['slide'] = locator['page']


def normalize_document(document: object, *, parser_version: str, max_sections: int,
                       max_text_chars: int) -> ParsedDocument:
    sections, warnings, tables = [], [], []
    heading = ''
    headings = []
    chars = 0
    for item, _level in document.iterate_items():
        label = getattr(item.label, 'value', item.label)
        locator = {'item': str(item.self_ref)}
        provenance = getattr(item, 'prov', [])
        if provenance:
            pages = sorted({prov.page_no for prov in provenance})
            locator['page'] = pages[0]
            if len(pages) > 1:
                locator['pages'] = pages
        office_context(document, item, locator)
        if label == 'table':
            if 'sheet' in locator and provenance:
                # Docling's XLSX bbox is the zero-based worksheet anchor,
                # unlike PDF pixel coordinates. Preserve it before row expansion.
                bbox = getattr(provenance[0], 'bbox', None)
                if bbox is not None:
                    for name, value in (('row_start', bbox.t), ('column_start', bbox.l)):
                        if value >= 0 and float(value).is_integer():
                            locator[name] = int(value) + 1
            table = structured_table(item, locator, f't-{len(tables)+1}')
            tables.append(table)
            if len(tables) > 1000 or sum(len(t.cells) for t in tables) > 200000:
                raise UnsupportedDocument('parser_table_structure_limit')
            entries = table_sections(item, {**locator, 'table_id': table.table_id}, heading)
        else:
            text = clean_text(getattr(item, 'text', ''))
            if label in ('section_header', 'title') and text:
                level = 1 if label == 'title' else getattr(item, 'level', max(1, _level + 1))
                level = level if isinstance(level, int) and 1 <= level <= 100 else 1
                headings = [(depth, value) for depth, value in headings if depth < level]
                headings.append((level, text))
                heading = ' / '.join(value for _, value in headings)
            if label == 'picture':
                warnings.append('图片项仅包含解析引擎实际识别内容，未生成描述')
            entries = (Section(text, locator, heading),) if text else ()
        for section in entries:
            chars += len(section.text) + len(section.heading)
            if len(sections) >= max_sections or chars > max_text_chars:
                raise UnsupportedDocument('parser_output_limit')
            sections.append(section)
    if not sections:
        raise UnsupportedDocument('parser_no_searchable_text')
    return ParsedDocument(tuple(sections), tuple(dict.fromkeys(warnings)), parser_version, tuple(tables), (), 2)
