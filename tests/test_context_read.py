import importlib.util

import pytest

from knowseam.domain import NotFound


def context_module():
    assert importlib.util.find_spec("knowseam.context"), "context capability missing"
    from knowseam import context

    return context


def test_merge_neighbor_windows_without_duplicate_evidence():
    m = context_module()
    request = m.ContextRequest(document_id="d", version_id="v", chunk_ids=["c1", "c2"], max_chars=1000)
    chunks = [{"chunk_id": "c" + str(i), "ordinal": i, "text": str(i), "locator": {}} for i in range(4)]
    result = m.assemble_context(request, chunks, [], [])
    assert [r["chunk_id"] for r in result["ranges"]] == ["c0", "c1", "c2", "c3"]
    assert result["context_status"] == "legacy_neighbors"


def test_legacy_index_cannot_fake_parent():
    m = context_module()
    request = m.ContextRequest(document_id="d", version_id="v", chunk_ids=["c"], mode="section")
    with pytest.raises(ValueError, match="structure_unavailable"):
        m.assemble_context(
            request, [{"chunk_id": "c", "ordinal": 0, "text": "augmented", "locator": {}}], [], []
        )


def test_budget_truncates_raw_section_not_citations():
    m = context_module()
    request = m.ContextRequest(
        document_id="d", version_id="v", chunk_ids=["c"], mode="section", max_chars=1000
    )
    result = m.assemble_context(
        request,
        [{"chunk_id": "c", "ordinal": 0}],
        [{"section_id": "s", "ordinal": 0, "text": "甲" * 2000, "heading": "", "locator": {}}],
        [{"chunk_id": "c", "section_id": "s", "start_char": 100, "end_char": 200}],
    )
    assert result["truncated"]
    assert len(result["ranges"][0]["text"]) == 1000
    assert result["ranges"][0]["end_char"] == 1000


def test_context_missing_chunk_does_not_return_other_contents():
    m = context_module()
    request = m.ContextRequest(document_id="d", version_id="v", chunk_ids=["missing"])
    with pytest.raises(NotFound):
        m.assemble_context(request, [{"chunk_id": "c", "ordinal": 0}], [], [])
