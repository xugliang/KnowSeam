"""Retrieval CONTRACT tests; deterministic reranker doubles are not pretrained models."""

import asyncio
from dataclasses import replace

import pytest

from knowseam.domain import Reranker, SearchHit
from knowseam.models import ModelError
from knowseam.retrieval import RetrievalError, deduplicate_hits, reciprocal_rank_fusion, rerank_hits


def hit(chunk, score=1.0, **kwargs):
    data = dict(document_id="doc", version_id="v1", chunk_id=chunk, collection_id="public",
                title="产品资料", text="来源内容" + chunk, source_url="https://company.example/wiki/doc",
                locator={"page": 1}, score=score)
    data.update(kwargs)
    return SearchHit(**data)


class ContractReranker(Reranker):
    model_id = "CONTRACT-ONLY-NOT-A-MODEL"

    def __init__(self, values):
        self.values = values
        self.calls = []

    async def score(self, query, documents):
        self.calls.append((query, documents))
        return self.values


def test_rrf_uses_rank_not_incommensurable_channel_scores():
    a, b, c = hit("a", .1), hit("b", 999), hit("c", -100)
    result = reciprocal_rank_fusion([[a, b], [b, c]], k=60)
    assert [value.chunk_id for value in result] == ["b", "a", "c"]
    assert result[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert b.score == 999  # input dataclasses are not modified


def test_duplicate_in_channel_does_not_inflate_rank_score():
    a, b = hit("a"), hit("b")
    result = reciprocal_rank_fusion([[a, a, b], [b]])
    assert result[0].chunk_id == "b"
    assert result[0].score == pytest.approx(1 / 62 + 1 / 61)


def test_citations_versions_and_collection_identity_not_collapsed():
    a = hit("a", text="完全相同")
    items = [a, replace(a, version_id="v2"), replace(a, document_id="another"), replace(a, collection_id="other")]
    assert len(deduplicate_hits(items + [a])) == 4
    assert len(reciprocal_rank_fusion([items], limit=10)) == 4


def test_rrf_boundaries_empty_and_deterministic_ties():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[hit("a")]], limit=0) == []
    assert reciprocal_rank_fusion([[hit("b")], [hit("a")]])[0].chunk_id == "a"
    for kwargs in [{"k": 0}, {"limit": -1}, {"limit": True}]:
        with pytest.raises(RetrievalError):
            reciprocal_rank_fusion([], **kwargs)
    with pytest.raises(RetrievalError):
        reciprocal_rank_fusion([[hit("a")] * 1001])
    with pytest.raises(RetrievalError):
        deduplicate_hits([hit("")])


def test_rerank_is_bounded_reorders_without_losing_source():
    hits = [hit(str(index)) for index in range(5)]
    ranker = ContractReranker([.1, .9, .4])
    result = asyncio.run(rerank_hits("如何使用", hits, ranker, candidate_limit=3, limit=2))
    assert [value.chunk_id for value in result] == ["1", "2"]
    assert len(ranker.calls[0][1]) == 3
    assert ranker.calls[0][1][0] == "产品资料\n来源内容0"
    assert result[0].locator == {"page": 1}
    assert result[0].source_url == hits[1].source_url
    assert result[0].score == .9


def test_rerank_ties_preserve_fused_order_and_duplicates_removed():
    ranker = ContractReranker([.3, .3])
    a, b = hit("a"), hit("b")
    result = asyncio.run(rerank_hits("查询", [b, b, a], ranker))
    assert [value.chunk_id for value in result] == ["b", "a"]
    assert len(ranker.calls[0][1]) == 2


@pytest.mark.parametrize("scores", [[.3], [float("nan"), 1], ["0.3", 1]])
def test_invalid_reranker_results_are_explicit_failure(scores):
    with pytest.raises(ModelError):
        asyncio.run(rerank_hits("查询", [hit("a"), hit("b")], ContractReranker(scores)))


def test_rerank_timeout_empty_and_input_limits():
    class Slow(ContractReranker):
        async def score(self, query, documents):
            await asyncio.sleep(.1)
            return [.1]

    with pytest.raises(RetrievalError, match="timed out"):
        asyncio.run(rerank_hits("查询", [hit("a")], Slow([]), timeout=.001))
    ranker = ContractReranker([])
    assert asyncio.run(rerank_hits("查询", [], ranker)) == []
    assert not ranker.calls
    with pytest.raises(RetrievalError):
        asyncio.run(rerank_hits("查询", [], ranker, limit=41, candidate_limit=40))
    with pytest.raises(RetrievalError, match="chunking"):
        asyncio.run(rerank_hits("查询", [hit("a", text="长" * 65536)], ranker))
