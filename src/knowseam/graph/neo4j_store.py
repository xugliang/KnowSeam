"""Neo4j evidence projection, fenced by the authoritative event sequence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from contextlib import contextmanager

from .config import GraphConfig
from .domain import EvidenceRef, GraphDocument, GraphEdge, GraphStore, normalized_name
from .extraction import validate_extraction
from .query import QueryGraphStore

TRANSACTION_TIMEOUT = 30
MAX_DOCUMENT_CHUNKS = 10000


class GraphStoreError(RuntimeError):
    """Sanitized graph storage failure, safe to expose in worker diagnostics."""


def _identity(parts) -> str:
    value = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(*values) -> None:
    if any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None
           for value in values):
        raise GraphStoreError("graph operation requires valid scoped identifiers")


def _sequence(value) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 2**63 - 1:
        raise GraphStoreError("graph event sequence must be a positive bounded integer")


def _limit(value) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise GraphStoreError("graph query limit must be within 1..100")


def _native_datetime(value):
    return value.to_native() if value is not None and hasattr(value, "to_native") else value


class Neo4jGraphStore(GraphStore, QueryGraphStore):
    def __init__(self, config: GraphConfig, *, driver=None):
        self.config = config
        if not config.enabled:
            raise GraphStoreError("Neo4j graph must be explicitly enabled")
        if driver is None:
            try:
                from neo4j import GraphDatabase
                driver = GraphDatabase.driver(config.uri, auth=(config.username, config.password.get_secret_value()),
                                              max_connection_pool_size=4, connection_timeout=5,
                                              connection_acquisition_timeout=10, telemetry_disabled=True,
                                              max_transaction_retry_time=15)
            except Exception:
                raise GraphStoreError("Neo4j graph driver could not be initialized") from None
        self._driver = driver

    @contextmanager
    def _transaction(self):
        try:
            with self._driver.session(database=self.config.database) as session:
                with session.begin_transaction(timeout=TRANSACTION_TIMEOUT) as transaction:
                    yield transaction
                    transaction.commit()
        except GraphStoreError:
            raise
        except Exception:
            raise GraphStoreError("Neo4j graph operation failed") from None

    def initialize(self) -> None:
        statements = (
            "CREATE CONSTRAINT knowseam_document_scope IF NOT EXISTS FOR (d:KSDocument) "
            "REQUIRE (d.company_id, d.collection_id, d.document_id) IS UNIQUE",
            "CREATE CONSTRAINT knowseam_entity_id IF NOT EXISTS FOR (e:KSEntity) REQUIRE e.entity_id IS UNIQUE",
            "CREATE CONSTRAINT knowseam_mention_id IF NOT EXISTS FOR (m:KSMention) REQUIRE m.mention_id IS UNIQUE",
            "CREATE CONSTRAINT knowseam_fact_id IF NOT EXISTS FOR (f:KSFact) REQUIRE f.fact_id IS UNIQUE",
        )
        # Schema and data transactions are intentionally separate.
        for statement in statements:
            with self._transaction() as transaction:
                transaction.run(statement).consume()

    def ready(self) -> bool:
        try:
            self._driver.verify_connectivity()
            return True
        except Exception:
            return False

    @staticmethod
    def _lock(transaction, scope):
        record = transaction.run(
            "MERGE (d:KSDocument {company_id:$company_id, collection_id:$collection_id, document_id:$document_id}) "
            "ON CREATE SET d.event_sequence = -1, d.lock_counter = 0 "
            "SET d.lock_counter = coalesce(d.lock_counter, 0) + 1 "
            "RETURN d.event_sequence AS event_sequence", **scope).single()
        if record is None:
            raise GraphStoreError("graph document fence could not be acquired")
        return record["event_sequence"]

    @staticmethod
    def _clear_evidence(transaction, scope):
        transaction.run(
            "MATCH (m:KSMention {company_id:$company_id, collection_id:$collection_id, document_id:$document_id}) "
            "DETACH DELETE m", **scope).consume()
        transaction.run(
            "MATCH (f:KSFact {company_id:$company_id, collection_id:$collection_id, document_id:$document_id}) "
            "DETACH DELETE f", **scope).consume()

    def _rows(self, document):
        _scope(document.company_id, document.collection_id, document.document_id, document.version_id)
        _sequence(document.event_sequence)
        if (len(document.chunks) > MAX_DOCUMENT_CHUNKS or len(document.chunks) != len(document.extractions)
                or document.collection_id not in self.config.collections
                or re.fullmatch(r"[a-f0-9]{64}", document.projection_key) is None):
            raise GraphStoreError("graph document has invalid extraction or projection metadata")
        entities = {}
        mentions = []
        facts = []
        chunk_ids = set()
        ontology = self.config.collections[document.collection_id]
        for chunk, extraction in zip(document.chunks, document.extractions, strict=True):
            evidence = chunk.evidence
            if ((evidence.company_id, evidence.collection_id, evidence.document_id, evidence.version_id)
                    != (document.company_id, document.collection_id, document.document_id, document.version_id)
                    or evidence.chunk_id in chunk_ids):
                raise GraphStoreError("graph chunk evidence differs from the document scope")
            chunk_ids.add(evidence.chunk_id)
            try:
                extraction = validate_extraction(chunk, ontology, extraction)
            except Exception:
                raise GraphStoreError("graph document contains invalid extraction evidence") from None
            refs = evidence.model_dump()
            key_ids = {}
            for entity in extraction.entities:
                entity_id = _identity((document.company_id, document.collection_id, entity.type,
                                       normalized_name(entity.name), normalized_name(entity.qualifier)))
                key_ids[entity.key] = entity_id
                entities[entity_id] = {"entity_id": entity_id, "company_id": document.company_id,
                                       "collection_id": document.collection_id, "entity_type": entity.type,
                                       "canonical": normalized_name(entity.name), "name": entity.name,
                                       "qualifier": normalized_name(entity.qualifier)}
                mentions.append({**refs, "mention_id": _identity((*refs.values(), entity.key, entity_id)),
                                 "entity_id": entity_id, "quote": entity.quote})
            for index, relation in enumerate(extraction.relations):
                facts.append({**refs, "fact_id": _identity((*refs.values(), index)),
                              "subject_id": key_ids[relation.subject], "object_id": key_ids[relation.object],
                              "relation_type": relation.type, "quote": relation.quote,
                              "explicit": relation.explicit, "confidence": relation.confidence,
                              "valid_from": relation.valid_from, "valid_until": relation.valid_until})
        return list(entities.values()), mentions, facts

    def replace_document(self, document: GraphDocument) -> None:
        entities, mentions, facts = self._rows(document)
        scope = {"company_id": document.company_id, "collection_id": document.collection_id,
                 "document_id": document.document_id}
        with self._transaction() as transaction:
            if document.event_sequence <= self._lock(transaction, scope):
                return
            self._clear_evidence(transaction, scope)
            transaction.run("UNWIND $rows AS row MERGE (e:KSEntity {entity_id:row.entity_id}) SET e += row",
                            rows=entities).consume()
            transaction.run(
                "UNWIND $rows AS row MATCH (e:KSEntity {entity_id:row.entity_id}) "
                "CREATE (m:KSMention) SET m += row CREATE (m)-[:KS_MENTIONS]->(e)", rows=mentions).consume()
            transaction.run(
                "UNWIND $rows AS row MATCH (s:KSEntity {entity_id:row.subject_id}), "
                "(o:KSEntity {entity_id:row.object_id}) CREATE (f:KSFact) SET f += row "
                "CREATE (f)-[:KS_SUBJECT]->(s) CREATE (f)-[:KS_OBJECT]->(o)", rows=facts).consume()
            transaction.run(
                "MATCH (d:KSDocument {company_id:$company_id, collection_id:$collection_id, document_id:$document_id}) "
                "SET d.event_sequence=$event_sequence, d.version_id=$version_id, d.projection_key=$projection_key, "
                "d.extractor_version=$extractor_version, d.model_id=$model_id, d.model_revision=$model_revision, "
                "d.deleted=false", **scope, event_sequence=document.event_sequence, version_id=document.version_id,
                projection_key=document.projection_key, extractor_version=document.extractor_version,
                model_id=document.model_id, model_revision=document.model_revision).consume()

    def remove_document(self, company_id: str, collection_id: str, document_id: str, event_sequence: int) -> None:
        _scope(company_id, collection_id, document_id)
        _sequence(event_sequence)
        scope = {"company_id": company_id, "collection_id": collection_id, "document_id": document_id}
        with self._transaction() as transaction:
            if event_sequence <= self._lock(transaction, scope):
                return
            self._clear_evidence(transaction, scope)
            transaction.run(
                "MATCH (d:KSDocument {company_id:$company_id, collection_id:$collection_id, document_id:$document_id}) "
                "SET d.event_sequence=$event_sequence, d.deleted=true", **scope, event_sequence=event_sequence).consume()

    def _projection_key(self, collection_id):
        try:
            return self.config.projection_key(collection_id)
        except ValueError:
            raise GraphStoreError("graph collection has no configured projection") from None

    def seed_entities(self, company_id: str, collection_id: str, evidence: Sequence[EvidenceRef],
                      limit: int) -> list[str]:
        _scope(company_id, collection_id)
        _limit(limit)
        if len(evidence) > 100:
            raise GraphStoreError("graph seed evidence exceeds the input limit")
        if not evidence:
            return []
        if any(not isinstance(ref, EvidenceRef) or ref.company_id != company_id or ref.collection_id != collection_id
               for ref in evidence):
            raise GraphStoreError("graph seed evidence must match the authorized scope")
        with self._transaction() as transaction:
            records = transaction.run(
                "UNWIND $evidence AS ref MATCH (m:KSMention)-[:KS_MENTIONS]->(e:KSEntity) "
                "MATCH (d:KSDocument {company_id:$company_id, collection_id:$collection_id}) "
                "WHERE m.company_id=$company_id AND m.collection_id=$collection_id "
                "AND e.company_id=$company_id AND e.collection_id=$collection_id "
                "AND m.document_id=ref.document_id AND m.version_id=ref.version_id AND m.chunk_id=ref.chunk_id "
                "AND d.document_id=m.document_id AND d.version_id=m.version_id "
                "AND d.projection_key=$projection_key AND d.deleted=false "
                "RETURN DISTINCT e.entity_id AS entity_id ORDER BY entity_id LIMIT $limit",
                company_id=company_id, collection_id=collection_id, evidence=[ref.model_dump() for ref in evidence],
                projection_key=self._projection_key(collection_id), limit=limit)
            return [record["entity_id"] for record in records]

    def neighbors(self, company_id: str, collection_id: str, entity_ids: Sequence[str], limit: int) -> list[GraphEdge]:
        _scope(company_id, collection_id)
        _limit(limit)
        if len(entity_ids) > 100 or any(not isinstance(value, str) or not value or len(value) > 128 for value in entity_ids):
            raise GraphStoreError("graph entity input exceeds bounds")
        if not entity_ids:
            return []
        with self._transaction() as transaction:
            records = transaction.run(
                "MATCH (s:KSEntity)<-[:KS_SUBJECT]-(f:KSFact)-[:KS_OBJECT]->(o:KSEntity) "
                "MATCH (d:KSDocument {company_id:$company_id, collection_id:$collection_id}) "
                "WHERE f.company_id=$company_id AND f.collection_id=$collection_id "
                "AND s.company_id=$company_id AND s.collection_id=$collection_id "
                "AND o.company_id=$company_id AND o.collection_id=$collection_id "
                "AND (s.entity_id IN $entity_ids OR o.entity_id IN $entity_ids) "
                "AND d.document_id=f.document_id AND d.version_id=f.version_id "
                "AND d.projection_key=$projection_key AND d.deleted=false "
                "RETURN f.company_id AS company_id, f.collection_id AS collection_id, "
                "f.document_id AS document_id, f.version_id AS version_id, f.chunk_id AS chunk_id, "
                "s.entity_id AS subject_id, o.entity_id AS object_id, f.relation_type AS relation_type, "
                "f.quote AS quote, f.explicit AS explicit, f.confidence AS confidence, "
                "f.valid_from AS valid_from, f.valid_until AS valid_until ORDER BY f.fact_id LIMIT $limit",
                company_id=company_id, collection_id=collection_id, entity_ids=list(entity_ids),
                projection_key=self._projection_key(collection_id), limit=limit)
            edges = []
            for record in records:
                evidence = EvidenceRef(**{name: record[name] for name in EvidenceRef.model_fields})
                if evidence.company_id != company_id or evidence.collection_id != collection_id:
                    raise GraphStoreError("graph returned evidence outside the requested scope")
                edges.append(GraphEdge(evidence, record["subject_id"], record["object_id"], record["relation_type"],
                                       record["quote"], record["explicit"], record["confidence"],
                                       _native_datetime(record["valid_from"]), _native_datetime(record["valid_until"])))
            return edges

    def close(self) -> None:
        try:
            self._driver.close()
        except Exception:
            raise GraphStoreError("Neo4j graph driver could not be closed") from None

    def neighbors_authorized(self, company_id, collection_id, entity_ids, facts, limit):
        _scope(company_id,collection_id)
        _limit(limit)
        if len(facts)>100 or len(entity_ids)>100 or any(f.evidence.company_id!=company_id or
                f.evidence.collection_id!=collection_id for f in facts):
            raise GraphStoreError('invalid_authorized_fact_slice')
        if not facts:
            return []
        rows = [dict(f.evidence.model_dump(),i=i,subject_id=f.subject_id,object_id=f.object_id,
                     relation=f.relation_type,quote=f.quote) for i,f in enumerate(facts)]
        with self._transaction() as tx:
            records = tx.run('UNWIND $facts AS row MATCH (f:KSFact {company_id:$company_id,collection_id:$collection_id}) '
                'MATCH (d:KSDocument {company_id:$company_id,collection_id:$collection_id}) '
                'WHERE f.document_id=row.document_id AND f.version_id=row.version_id AND f.chunk_id=row.chunk_id '
                'AND f.subject_id=row.subject_id AND f.object_id=row.object_id AND f.relation_type=row.relation '
                'AND f.quote=row.quote AND f.explicit=true AND d.document_id=f.document_id AND d.version_id=f.version_id '
                'AND d.deleted=false AND d.projection_key=$projection_key '
                'AND (size($entities)=0 OR row.subject_id IN $entities OR row.object_id IN $entities) '
                'RETURN DISTINCT row.i AS i ORDER BY i LIMIT $limit',
                company_id=company_id,collection_id=collection_id,facts=rows,entities=list(entity_ids),
                projection_key=self._projection_key(collection_id),limit=limit)
            indices = [record['i'] for record in records]
        if any(type(i) is not int or not 0<=i<len(facts) for i in indices):
            raise GraphStoreError('invalid_authorized_graph_response')
        return [facts[i] for i in indices]
