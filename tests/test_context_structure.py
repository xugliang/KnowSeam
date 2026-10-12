from knowseam.chunking import chunk_document
from knowseam.domain import ParsedDocument, Section


def test_original_section_not_embedding_prefix():
    raw = '  甲\r\n乙\r丙  '
    chunks = chunk_document(ParsedDocument((Section(raw, heading='generated-heading'),)), source_spans=True)
    chunk = chunks[0]
    assert chunk.text == '甲\n乙\n丙'
    assert chunk.locator['section_id'] == 's-0'
    assert raw[chunk.locator['char_start']:chunk.locator['char_end']] == '甲\r\n乙\r丙'
    assert chunk.spans[0].section_id == 's-0'


def test_augmented_table_text_is_not_claimed_as_raw_span():
    raw = '甲' * 500
    parsed = ParsedDocument((Section(raw, {'table': 1, 'headers': ['单价'], 'row_context': '产品名'}),))
    chunks = chunk_document(parsed, max_chars=200, overlap=20, source_spans=True)
    assert '表头' in chunks[0].text
    for chunk in chunks:
        span = chunk.spans[0]
        assert raw[span.start_char:span.end_char] in chunk.text
        assert '表头' not in raw[span.start_char:span.end_char]


def test_default_chunks_keep_old_locator_contract():
    chunk = chunk_document(ParsedDocument((Section('  甲\r\n乙 '),)))[0]
    assert chunk.locator == {'section': 0, 'char_start': 0, 'char_end': 3}


def test_source_mapping_does_not_allocate_python_object_per_character():
    import tracemalloc
    raw = ' ' * 400000 + '甲\r\n乙'
    tracemalloc.start()
    try:
        chunks = chunk_document(ParsedDocument((Section(raw),)), source_spans=True)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    assert raw[chunks[0].spans[0].start_char:chunks[0].spans[0].end_char] == '甲\r\n乙'


def test_many_crlf_boundaries_and_overlaps_remain_exact():
    raw = ' \r\n' + ('甲\r\n乙\r丙\n😀' * 100) + ' \r\n'
    chunks = chunk_document(ParsedDocument((Section(raw),)), max_chars=200, overlap=100, source_spans=True)
    for chunk in chunks:
        span = chunk.spans[0]
        assert raw[span.start_char:span.end_char].replace('\r\n','\n').replace('\r','\n') == chunk.text
