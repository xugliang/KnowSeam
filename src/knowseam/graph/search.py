"""Bounded graph expansion; preserve all path evidence through final reranking."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from .domain import EvidenceRef


def hit_key(value):
    return value.collection_id, value.document_id, value.version_id, value.chunk_id


class GraphCandidates(list):
    """Internal support metadata, never serialized in the public search contract."""

    def __init__(self, hits=(), *, references=None, paths=None):
        super().__init__(hits)
        self.references = references or {}
        self.paths = paths or {}


class GraphSearch:
    def __init__(self, repository, graph_store, config):
        self.repository, self.graph_store, self.config = repository, graph_store, config

    async def revalidate(self, principal, hits, candidates, independent_keys=frozenset()):
        """One authoritative read validates seed, bridges AND returned chunks together."""
        references = dict(candidates.references)
        for hit in hits:
            if hit_key(hit) in independent_keys:
                references[hit_key(hit)] = EvidenceRef(
                    company_id=principal.company_id, collection_id=hit.collection_id,
                    document_id=hit.document_id, version_id=hit.version_id, chunk_id=hit.chunk_id)
        current = await asyncio.to_thread(self.repository.graph_evidence, principal,
                                         list(references.values()))
        readable = {hit_key(hit): hit for hit in current}
        output = []
        for hit in hits:
            key = hit_key(hit)
            if key in independent_keys and key in readable:
                output.append(replace(readable[key], score=hit.score))
            elif key in readable and any(path <= readable.keys() for path in candidates.paths.get(key, ())):
                output.append(replace(readable[key], score=hit.score))
        return output

    async def expand(self, principal, collections, seeds, *, query=None, options=None):
        if options is not None and options.get('strategy','seed') != 'seed':
            raise ValueError('query_aware_adapter_required')
        seeds = await asyncio.to_thread(self.repository.revalidate_hits, principal, seeds)
        seeds = seeds[:self.config.candidate_limit]
        output, seen, references, paths = [], set(), {}, {}
        remaining = self.config.candidate_limit
        for collection in collections:
            if collection not in self.config.collections or remaining <= 0:
                continue
            selected = [hit for hit in seeds if hit.collection_id == collection][:remaining]
            if not selected:
                continue
            refs = [EvidenceRef(company_id=principal.company_id, collection_id=collection,
                                document_id=hit.document_id, version_id=hit.version_id, chunk_id=hit.chunk_id)
                    for hit in selected]
            references.update((hit_key(ref), ref) for ref in refs)
            frontier = await asyncio.to_thread(self.graph_store.seed_entities, principal.company_id,
                                             collection, refs, limit=remaining)
            frontier = list(dict.fromkeys(frontier))[:remaining]
            # Store returns scoped IDs, not seed provenance. Conservatively require
            # all selected seed evidence; revocation may reduce recall, never widen access.
            dependencies = {entity: frozenset(hit_key(ref) for ref in refs) for entity in frontier}
            visited = set(frontier)
            for _ in range(self.config.hops):
                if not frontier or remaining <= 0:
                    break
                edges = await asyncio.to_thread(self.graph_store.neighbors, principal.company_id,
                                               collection, frontier, limit=remaining)
                edges = edges[:remaining]
                remaining -= len(edges)  # Unauthorized candidates also consume the exploration budget.
                now = datetime.now(timezone.utc)
                edges = [edge for edge in edges
                         if edge.evidence.company_id == principal.company_id
                         and edge.evidence.collection_id == collection
                         and (edge.explicit or self.config.include_inferred)
                         and edge.confidence >= self.config.min_confidence
                         and (edge.valid_from is None or edge.valid_from <= now)
                         and (edge.valid_until is None or edge.valid_until > now)
                         and (edge.subject_id in frontier or edge.object_id in frontier)]
                hits = await asyncio.to_thread(self.repository.graph_evidence, principal,
                                               [edge.evidence for edge in edges])
                authorized = {hit_key(hit): hit for hit in hits}
                next_frontier = []
                for edge in edges:
                    key = hit_key(edge.evidence)
                    hit = authorized.get(key)
                    if hit is None or not edge.quote or edge.quote not in hit.text:
                        continue
                    references[key] = edge.evidence
                    source = edge.subject_id if edge.subject_id in frontier else edge.object_id
                    path = dependencies[source] | {key}
                    paths.setdefault(key, []).append(path)
                    # Only an authorized edge may enter the next hop.
                    for endpoint in (edge.subject_id, edge.object_id):
                        if endpoint not in visited:
                            visited.add(endpoint)
                            dependencies[endpoint] = path
                            next_frontier.append(endpoint)
                    if key not in seen:
                        seen.add(key)
                        output.append(hit)
                frontier = next_frontier[:remaining]
        candidates = GraphCandidates(output, references=references, paths=paths)
        current = await self.revalidate(principal, candidates, candidates)
        return GraphCandidates(current, references=references, paths=paths)
