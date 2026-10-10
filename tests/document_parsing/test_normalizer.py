from types import SimpleNamespace as NS

import pytest

from knowseam.chunking import chunk_document
from knowseam.domain import UnsupportedDocument


class Document:
    def __init__(self, items):
        self.items = items
    def iterate_items(self):
        return iter((item, 0) for item in self.items)


def cell(text, row, col, *, header=False, end_col=None):
    return NS(text=text, start_row_offset_idx=row, end_row_offset_idx=row+1,
              start_col_offset_idx=col, end_col_offset_idx=end_col or col+1, column_header=header)


def test_structured_table_and_provenance_without_fabrication():
    from knowseam.document_parsing.normalizer import normalize_document
    table = NS(label='table', self_ref='#/tables/0', prov=[NS(page_no=2)],
               data=NS(num_rows=3, num_cols=2, table_cells=[cell('产品',0,0,header=True), cell('数量',0,1,header=True),
                    cell('示例产品',1,0), cell('5',1,1), cell('',2,0), cell('',2,1)]))
    items = [NS(label='section_header', text='功能说明', self_ref='#/texts/0', prov=[NS(page_no=1)]), table]
    parsed = normalize_document(Document(items), parser_version='x', max_sections=10, max_text_chars=10000)
    assert parsed.sections[0].heading == '功能说明'
    row = parsed.sections[1]
    assert row.text == '产品: 示例产品 | 数量: 5'
    assert row.locator['page'] == 2 and row.locator['row'] == 2
    assert len(parsed.sections) == 2


def test_long_table_row_keeps_headers_and_key_context():
    from knowseam.document_parsing.normalizer import normalize_document
    table = NS(label='table', self_ref='#/tables/0', prov=[],
               data=NS(num_rows=2, num_cols=2, table_cells=[cell('编号',0,0,header=True),cell('内容',0,1,header=True),
                     cell('KEY-1',1,0),cell('很长的内容'*500,1,1)]))
    parsed = normalize_document(Document([table]), parser_version='x', max_sections=10, max_text_chars=10000)
    chunks = chunk_document(parsed, max_chars=300, overlap=20)
    assert len(chunks) > 2
    assert all('编号' in chunk.embedding_text and 'KEY-1' in chunk.embedding_text and '内容' in chunk.embedding_text for chunk in chunks)
    assert all(chunk.locator['row'] == 2 for chunk in chunks)
    assert all(len(chunk.text) <= 300 for chunk in chunks)


def test_limits_empty_and_picture_warning():
    from knowseam.document_parsing.normalizer import normalize_document
    with pytest.raises(UnsupportedDocument):
        normalize_document(Document([]), parser_version='x', max_sections=10, max_text_chars=100)
    items = [NS(label='text', text='x'*101, self_ref='x', prov=[])]
    with pytest.raises(UnsupportedDocument):
        normalize_document(Document(items), parser_version='x', max_sections=10, max_text_chars=100)


def test_wide_header_does_not_drop_row_identity_in_long_chunks():
    from knowseam.domain import ParsedDocument, Section
    parsed = ParsedDocument((Section('内容'*600, locator={
        'table': 't', 'headers': ['非常宽的字段名'*100, '内容'],
        'row_context': '编号: KEY-IMPORTANT'}),))
    chunks = chunk_document(parsed, max_chars=300, overlap=20)
    assert all('KEY-IMPORTANT' in c.text for c in chunks)
    assert all(len(c.text) <= 300 for c in chunks)


def test_office_locators_keep_sheet_name_and_slide_number():
    from knowseam.document_parsing.normalizer import normalize_document
    group = NS(label='sheet', name='功能表', parent=None)
    item = NS(label='text', text='单元格内容', self_ref='#/texts/0',
              parent=NS(resolve=lambda doc: group), prov=[NS(page_no=2)])
    doc = Document([item])
    parsed = normalize_document(doc, parser_version='x', max_sections=10, max_text_chars=1000)
    assert parsed.sections[0].locator['sheet'] == '功能表'
    doc.origin = NS(mimetype='application/vnd.openxmlformats-officedocument.presentationml.presentation')
    item.parent = None
    parsed = normalize_document(doc, parser_version='x', max_sections=10, max_text_chars=1000)
    assert parsed.sections[0].locator['slide'] == 2
    # The upstream PPTX backend normalizes origin to the first mime alias.
    doc.origin.mimetype = 'application/vnd.openxmlformats-officedocument.presentationml.template'
    parsed = normalize_document(doc, parser_version='x', max_sections=10, max_text_chars=1000)
    assert parsed.sections[0].locator['slide'] == 2


def test_excel_offsets_and_merged_header_band_preserve_leaf_fields():
    from knowseam.document_parsing.normalizer import normalize_document
    group = NS(label='sheet', name='Revenue', parent=None)
    product = cell('Product', 0, 0, header=True)
    product.end_row_offset_idx = 2
    table = NS(label='table', self_ref='#/tables/0',
               parent=NS(resolve=lambda doc: group),
               prov=[NS(page_no=1, bbox=NS(l=3, t=9))],
               data=NS(num_rows=3, num_cols=3, table_cells=[product,
                   cell('Finance', 0, 1, header=True, end_col=3),
                   cell('Revenue', 1, 1), cell('Cost', 1, 2),
                   cell('Widget', 2, 0), cell('5', 2, 1), cell('3', 2, 2)]))
    parsed = normalize_document(Document([table]), parser_version='x', max_sections=10, max_text_chars=1000)
    assert len(parsed.sections) == 1
    row = parsed.sections[0]
    assert row.text == 'Product: Widget | Finance / Revenue: 5 | Finance / Cost: 3'
    assert row.locator['row'] == 12
    assert row.locator['row_in_table'] == 3
    assert row.locator['column_start'] == 4


def test_heading_ancestry_and_long_field_value_are_unambiguous():
    from knowseam.document_parsing.normalizer import normalize_document
    items = [NS(label='section_header', level=1, text='Security', self_ref='a', prov=[]),
             NS(label='section_header', level=2, text='Policy', self_ref='b', prov=[]),
             NS(label='text', text='First policy', self_ref='c', prov=[]),
             NS(label='section_header', level=1, text='Products', self_ref='d', prov=[]),
             NS(label='section_header', level=2, text='Policy', self_ref='e', prov=[]),
             NS(label='text', text='Second policy', self_ref='f', prov=[])]
    parsed = normalize_document(Document(items), parser_version='x', max_sections=20, max_text_chars=1000)
    assert parsed.sections[2].heading == 'Security / Policy'
    assert parsed.sections[-1].heading == 'Products / Policy'
    table = NS(label='table', self_ref='t', prov=[],
        data=NS(num_rows=2, num_cols=3, table_cells=[cell('ID',0,0,header=True),
            cell('Description',0,1,header=True), cell('Limits',0,2,header=True),
            cell('KEY',1,0), cell('description-value '*100,1,1), cell('limits-value '*100,1,2)]))
    parsed = normalize_document(Document([table]), parser_version='x', max_sections=10, max_text_chars=10000)
    chunks = chunk_document(parsed, max_chars=300, overlap=20)
    for chunk in chunks:
        field = chunk.locator.get('field')
        assert field in {'ID', 'Description', 'Limits'}
        assert f'{field}: ' in chunk.text and 'KEY' in chunk.text
        if field == 'Description':
            assert 'limits-value' not in chunk.text
        elif field == 'Limits':
            assert 'description-value' not in chunk.text
