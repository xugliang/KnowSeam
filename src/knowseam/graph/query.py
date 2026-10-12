"""Query-aware bounded traversal of an authorized PostgreSQL fact slice."""

import asyncio
from dataclasses import replace
from abc import ABC, abstractmethod
import re
from typing import Annotated, Literal

from pydantic import Field
from ..contracts import RequestModel
from .domain import EvidenceRef, GraphEdge, normalized_name
from .search import GraphCandidates, GraphSearch, hit_key


class GraphQueryOptions(RequestModel):
    strategy: Literal["seed", "query_aware"] = "seed"
    include_paths: bool = False
    hops: Annotated[int, Field(ge=1, le=2)] = 1
    candidate_limit: Annotated[int, Field(ge=1, le=100)] = 40


def query_names(query, names):
    text = normalized_name(query)
    result = []
    for name in names:
        value = normalized_name(name)
        if re.fullmatch(r"[a-z0-9_.:-]+", value):
            match = re.search(r"(?<![a-z0-9_.:-])" + re.escape(value) + r"(?![a-z0-9_.:-])", text)
        else:
            match = value in text
        if match:
            result.append(name)
    return result


class QueryGraphStore(ABC):
    @abstractmethod
    def neighbors_authorized(self, company_id, collection_id, entity_ids, facts, limit): ...


class AuthorizedFactGraph(QueryGraphStore):
    """Exact traversal of the supplied authorized slice; no external graph required."""

    def neighbors_authorized(self, company_id, collection_id, entity_ids, facts, limit):
        if type(limit) is not int or not 1 <= limit <= 100 or len(facts) > 100:
            raise ValueError("query_graph_budget")
        return [
            f
            for f in facts
            if f.evidence.company_id == company_id
            and f.evidence.collection_id == collection_id
            and (not entity_ids or f.subject_id in entity_ids or f.object_id in entity_ids)
        ][:limit]


class PostgresFactCandidates:
    def __init__(self, store):
        self.store = store

    def authorized_fact_candidates(
        self, principal, collection, query, frontier, limit, excluded=(), *, only_ids=None
    ):
        if type(limit) is not int or not 1 <= limit <= 100 or len(frontier) > 100 or len(query) > 4096:
            raise ValueError("query_graph_budget")
        acl, args = self.store._acl(principal, [collection])
        ontology = getattr(self.store, "fact_ontologies", {}).get(collection)
        terms = re.findall(r"[a-z0-9_.:-]+", normalized_name(query))[:128]
        aliases = query_names(query, ontology.aliases) if ontology else []
        terms += [normalized_name(ontology.aliases[a]) for a in aliases]
        relations = query_names(query, ontology.relation_types) if ontology else []
        # Endpoint IDs have the same construction as Neo4j; do not accept model-invented entities.
        # Select a bounded authorized fact set before graph traversal, never all-graph top-k.
        with self.store.connect() as c:
            rows = c.execute(
                "WITH eligible AS MATERIALIZED (SELECT f.*,fs.version_id,fs.chunk_id,fs.quote,d.id AS document_id,"
                "k.company_id AS scope_company,k.id AS scope_collection FROM sk_fact_observations f "
                "JOIN sk_fact_supports fs ON fs.observation_id=f.id JOIN sk_versions v ON v.id=fs.version_id "
                "JOIN sk_documents d ON d.id=v.document_id AND d.active_version=v.id "
                "JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id "
                "JOIN sk_version_metadata m ON m.version_id=v.id WHERE "
                + acl
                + " AND f.company_id=k.company_id AND f.collection_id=k.id AND m.source_id=s.id AND m.config_hash=s.config_hash "
                "AND f.recorded_until IS NULL AND fs.recorded_until IS NULL "
                "AND (f.valid_from IS NULL OR f.valid_from<=now()) AND (f.valid_until IS NULL OR f.valid_until>now()) "
                "AND NOT(f.id=ANY(%s::text[]))), "
                "ranked AS (SELECT *,row_number() OVER(PARTITION BY id ORDER BY version_id,chunk_id) rank FROM eligible "
                "WHERE id=ANY(%s::text[]) OR ((NOT %s AND (lower(subject)=ANY(%s::text[]) OR lower(object)=ANY(%s::text[]) "
                "OR (subject !~ '^[A-Za-z0-9_.:-]+$' AND strpos(%s,lower(subject))>0) "
                "OR (object !~ '^[A-Za-z0-9_.:-]+$' AND strpos(%s,lower(object))>0))) "
                "OR subject_id=ANY(%s::text[]) OR object_id=ANY(%s::text[]))) "
                "SELECT * FROM ranked WHERE rank=1 ORDER BY (relation=ANY(%s::text[])) DESC,id LIMIT %s",
                (
                    *args,
                    [e.observation_id for e in excluded],
                    only_ids or [],
                    bool(frontier) or only_ids is not None,
                    terms,
                    terms,
                    normalized_name(query),
                    normalized_name(query),
                    list(frontier),
                    list(frontier),
                    relations,
                    limit,
                ),
            ).fetchall()
        result = []
        for row in rows:
            ref = EvidenceRef(
                company_id=principal.company_id,
                collection_id=collection,
                document_id=row["document_id"],
                version_id=row["version_id"],
                chunk_id=row["chunk_id"],
            )
            result.append(
                GraphEdge(
                    ref,
                    row["subject_id"],
                    row["object_id"],
                    row["relation"],
                    row["quote"],
                    True,
                    1.0,
                    row["valid_from"],
                    row["valid_until"],
                    row["id"],
                )
            )
        return result

    def revalidate_fact_edges(self, principal, edges):
        if len(edges) > 100:
            raise ValueError("query_graph_budget")
        current = []
        for collection in sorted({e.evidence.collection_id for e in edges}):
            selected = [e for e in edges if e.evidence.collection_id == collection]
            current.extend(
                self.authorized_fact_candidates(
                    principal, collection, "", (), 100, only_ids=[e.observation_id for e in selected]
                )
            )
        return [e for e in edges if e in current]


class QueryAwareSearch(GraphSearch):
    def __init__(self, repository, graph_store, config):
        super().__init__(repository, graph_store, config)
        self.provider = (
            repository
            if hasattr(repository, "authorized_fact_candidates")
            else PostgresFactCandidates(repository)
        )

    async def revalidate(self, principal, hits, candidates, independent_keys=frozenset()):
        edges = list(
            dict.fromkeys(e for paths in candidates.fact_paths.values() for path in paths for e in path)
        )
        current = await asyncio.to_thread(self.provider.revalidate_fact_edges, principal, edges)
        from .domain import EvidenceRef
        references = {hit_key(e.evidence): e.evidence for e in current}
        for hit in hits:
            if hit_key(hit) in independent_keys:
                references[hit_key(hit)] = EvidenceRef(
                    company_id=principal.company_id, collection_id=hit.collection_id,
                    document_id=hit.document_id, version_id=hit.version_id, chunk_id=hit.chunk_id)
        readable = await asyncio.to_thread(
            self.repository.graph_evidence, principal, list(references.values())
        )
        by_key = {hit_key(h): h for h in readable}
        valid_edges = [
            e
            for e in current
            if hit_key(e.evidence) in by_key and e.quote in by_key[hit_key(e.evidence)].text
        ]
        output, explanations = [], {}
        for hit in hits:
            key = hit_key(hit)
            paths = [p for p in candidates.fact_paths.get(key, ()) if all(e in valid_edges for e in p)]
            if paths:
                output.append(replace(by_key[key], score=hit.score))
                if candidates.include_paths:
                    explanations[key] = {
                        "derived": True,
                        "path": [
                            dict(
                                e.evidence.model_dump(),
                                observation_id=e.observation_id,
                                subject_id=e.subject_id,
                                object_id=e.object_id,
                                relation=e.relation_type,
                                quote=e.quote,
                                locator=by_key[hit_key(e.evidence)].locator,
                            )
                            for e in paths[0]
                        ],
                    }
            elif key in independent_keys and key in by_key:
                output.append(replace(by_key[key], score=hit.score))
        candidates.explanations = explanations
        return output

    async def expand(self, principal, collections, seeds, *, query=None, options=None):
        options = GraphQueryOptions.model_validate(
            options
            or {
                "strategy": "query_aware",
                "hops": self.config.hops,
                "candidate_limit": self.config.candidate_limit,
            }
        )
        if options.strategy != "query_aware" or not isinstance(query, str) or not query.strip():
            raise ValueError("query_aware_request_required")
        remaining = min(options.candidate_limit, self.config.candidate_limit)
        output, references, paths, fact_paths = [], {}, {}, {}
        seen = set()
        for collection in collections:
            if collection not in self.config.collections or remaining <= 0:
                continue
            frontier, dependencies, visited, used = [], {}, set(), []
            for _ in range(min(options.hops, self.config.hops)):
                facts = await asyncio.to_thread(
                    self.provider.authorized_fact_candidates,
                    principal,
                    collection,
                    query,
                    frontier,
                    remaining,
                    used,
                )
                edges = await asyncio.to_thread(
                    self.graph_store.neighbors_authorized,
                    principal.company_id,
                    collection,
                    frontier,
                    facts,
                    remaining,
                )
                # Projection responses are not an authority; intersect exact supplied candidates.
                edges = [e for e in edges if e in facts][:remaining]
                readable = await asyncio.to_thread(
                    self.repository.graph_evidence, principal, [e.evidence for e in edges]
                )
                current = {hit_key(h): h for h in readable}
                next_frontier = []
                for edge in edges:
                    if edge in used:
                        continue
                    used.append(edge)
                    remaining -= 1
                    key = hit_key(edge.evidence)
                    hit = current.get(key)
                    if hit is None or edge.quote not in hit.text:
                        continue
                    source = edge.subject_id if edge.subject_id in dependencies else edge.object_id
                    path = dependencies.get(source, ()) + (edge,)
                    fact_paths.setdefault(key, []).append(path)
                    paths.setdefault(key, []).append(frozenset(hit_key(e.evidence) for e in path))
                    references[key] = edge.evidence
                    if key not in seen:
                        seen.add(key)
                        output.append(hit)
                    for endpoint in (edge.subject_id, edge.object_id):
                        if endpoint not in visited:
                            visited.add(endpoint)
                            next_frontier.append(endpoint)
                            dependencies[endpoint] = path
                frontier = next_frontier[:100]
                if not frontier or remaining <= 0:
                    break
        candidates = GraphCandidates(output, references=references, paths=paths)
        candidates.fact_paths, candidates.include_paths = fact_paths, options.include_paths
        valid = await self.revalidate(principal, candidates, candidates)
        candidates[:] = valid
        return candidates
