"""Deterministic evidence splitting; no external services or model calls."""

from dataclasses import replace

import pytest

from knowseam.chunking import canonical, chunk_document, digest, parsed_hash
from knowseam.domain import ParsedDocument, Section


def test_chunking_is_deterministic_and_keeps_original_locators():
    parsed = ParsedDocument((Section("产品支持视频生成。", {"page": 3, "table": 1, "row": 2}, "Kino"),))
    first = chunk_document(parsed)
    assert first == chunk_document(parsed)
    assert first[0].locator == {"page": 3, "table": 1, "row": 2, "section": 0, "char_start": 0, "char_end": 9}
    assert first[0].embedding_text == "Kino\n产品支持视频生成。"


def test_long_section_has_no_uncovered_character_and_bounded_chunks():
    text = "甲" * 205 + "。" + "乙" * 380 + "。" + "丙" * 205
    chunks = chunk_document(ParsedDocument((Section(text, {"page": 1}),)), max_chars=240, overlap=30)
    covered = set()
    previous_end = 0
    for chunk in chunks:
        start, end = chunk.locator["char_start"], chunk.locator["char_end"]
        assert 0 < len(chunk.text) <= 240
        assert chunk.text == text[start:end]
        assert start <= previous_end
        covered.update(range(start, end))
        previous_end = end
    assert covered == set(range(len(text)))
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)


def test_distinct_pages_are_never_merged_and_keep_unique_chunk_identity():
    parsed = ParsedDocument((Section("同样文字", {"page": 1}), Section("同样文字", {"page": 2})))
    chunks = chunk_document(parsed)
    assert len(chunks) == 2
    assert chunks[0].content_hash == chunks[1].content_hash  # Embeddings may be reused.
    assert chunks[0].chunk_id != chunks[1].chunk_id  # Evidence identity cannot be collapsed.
    assert [chunk.locator["page"] for chunk in chunks] == [1, 2]


def test_heading_changes_embedding_fingerprint_but_locator_does_not():
    base = Section("功能介绍", {"row": 2}, "Kino")
    a = chunk_document(ParsedDocument((base,)))[0]
    b = chunk_document(ParsedDocument((replace(base, heading="示例产品"),)))[0]
    c = chunk_document(ParsedDocument((replace(base, locator={"row": 3}),)))[0]
    assert a.content_hash != b.content_hash
    assert a.content_hash == c.content_hash
    assert a.locator != c.locator


def test_document_hash_includes_order_locators_warnings_and_parser_version():
    first, second = Section("文本甲", {"page": 1}), Section("文本乙", {"page": 2})
    original = ParsedDocument((first, second))
    variants = [ParsedDocument((second, first)),
                ParsedDocument((replace(first, locator={"page": 3}), second)),
                replace(original, warnings=("图片未执行OCR",)),
                replace(original, parser_version="structured-v2")]
    for variant in variants:
        assert parsed_hash(original) != parsed_hash(variant)


def test_empty_sections_skipped_and_newlines_normalized():
    parsed = ParsedDocument((Section(" \n "), Section("  甲\r\n乙\r丙  ", {"line_start": 4})))
    chunks = chunk_document(parsed)
    assert len(chunks) == 1
    assert chunks[0].text == "甲\n乙\n丙"
    assert chunks[0].locator["section"] == 1
    assert chunks[0].ordinal == 0


@pytest.mark.parametrize("maximum,overlap", [(199, 10), (300, -1), (300, 300), (300, 301)])
def test_invalid_bounds_rejected(maximum, overlap):
    with pytest.raises(ValueError):
        chunk_document(ParsedDocument((Section("文字"),)), max_chars=maximum, overlap=overlap)


def test_canonical_hash_does_not_depend_on_mapping_insertion_order():
    assert canonical({"标题": "知识", "row": 2}) == canonical({"row": 2, "标题": "知识"})
    assert digest("公司资料") == digest("公司资料".encode("utf-8"))
