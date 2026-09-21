"""Pure fusion plus bounded reranking; authorization belongs to the repository.

Callers must only pass hits authorized for the authenticated principal. No model
may expand collections or override source/publication checks through this module.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from typing import Sequence

from .domain import KnowledgeError, Reranker, SearchHit
from .models import MAX_TEXT_CHARS, validate_scores, validate_texts


class RetrievalError(KnowledgeError):
    pass


def _bound(value: int, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RetrievalError(f"{name} must be an integer in [{low}, {high}]")
    return value


def hit_key(hit: SearchHit) -> tuple[str, str, str, str]:
    return hit.collection_id, hit.document_id, hit.version_id, hit.chunk_id


def deduplicate_hits(hits: Sequence[SearchHit], *, limit: int = 1000) -> list[SearchHit]:
    """Deduplicate stable chunk identities, retaining the first-ranked citation.

    Equal text from distinct documents is NOT the same source identity. Such hits
    may carry different citations and must not be merged across source versions.
    """
    _bound(limit, "limit", 0, 1000)
    if len(hits) > 1000:
        raise RetrievalError("too many retrieval candidates")
    output = []
    seen = set()
    for hit in hits:
        if len(output) >= limit:
            break
        if not isinstance(hit, SearchHit):
            raise RetrievalError("expected SearchHit")
        key = hit_key(hit)
        if not all(isinstance(item, str) and item for item in key):
            raise RetrievalError("search hits must contain stable source/version/chunk identities")
        if key not in seen:
            seen.add(key)
            output.append(hit)
    return output


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchHit]], *, limit: int = 60, k: int = 60,
) -> list[SearchHit]:
    """RRF: sum 1/(k + one-based rank); source scores need not share a scale."""
    _bound(limit, "limit", 0, 1000)
    _bound(k, "k", 1, 1000)
    if len(rankings) > 16 or any(len(ranking) > 1000 for ranking in rankings):
        raise RetrievalError("too many retrieval candidates")
    scores: dict[tuple[str, str, str, str], float] = {}
    originals: dict[tuple[str, str, str, str], SearchHit] = {}
    for ranking in rankings:
        # Duplicates in one channel never inflate its contribution or consume ranks.
        for rank, hit in enumerate(deduplicate_hits(ranking), start=1):
            key = hit_key(hit)
            originals.setdefault(key, hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores, key=lambda key: (-scores[key], key))
    return [replace(originals[key], score=scores[key]) for key in ordered[:limit]]


async def rerank_hits(
    query: str, hits: Sequence[SearchHit], reranker: Reranker, *, limit: int = 8,
    candidate_limit: int = 40, timeout: float = 30,
) -> list[SearchHit]:
    """Re-rank only the bounded fused shortlist; never silently fake a fallback."""
    _bound(limit, "limit", 0, 128)
    _bound(candidate_limit, "candidate_limit", 1, 128)
    if limit > candidate_limit:
        raise RetrievalError("limit must not exceed candidate_limit")
    if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
            or not math.isfinite(timeout) or not 0 < timeout <= 600):
        raise RetrievalError("timeout must be in (0, 600] seconds")
    validate_texts([query])
    if limit == 0:
        return []
    candidates = deduplicate_hits(hits, limit=candidate_limit)
    if not candidates:
        return []
    # A title helps disambiguate fragments but never replaces the source text.
    documents = [f"{hit.title}\n{hit.text}" if hit.title else hit.text for hit in candidates]
    if any(len(text) > MAX_TEXT_CHARS for text in documents):
        raise RetrievalError("candidate exceeds reranker character limit; fix chunking")
    validate_texts(documents)
    try:
        values = await asyncio.wait_for(reranker.score(query, documents), timeout=timeout)
    except TimeoutError:
        raise RetrievalError("reranker timed out") from None
    scores = validate_scores(values, len(candidates))
    # Stable ties retain the fused order.
    ordered = sorted(enumerate(candidates), key=lambda pair: (-scores[pair[0]], pair[0]))
    return [replace(hit, score=scores[index]) for index, hit in ordered[:limit]]
