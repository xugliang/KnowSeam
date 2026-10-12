"""PostgreSQL fact authority; graph stores are disposable projections."""
from itertools import combinations
from datetime import timezone

from ..chunking import canonical, digest
from .domain import normalized_name
from .extraction import validate_extraction
from .identity import entity_id


def conflicts(rows, single_valued):
    pairs = []
    for left, right in combinations(rows, 2):
        if (left['relation'] not in single_valued or
                (left['subject_id'],left['relation']) !=
                (right['subject_id'],right['relation']) or left['object_id'] == right['object_id']):
            continue
        if (left['valid_until'] is not None and right['valid_from'] is not None and left['valid_until'] <= right['valid_from']
                or right['valid_until'] is not None and left['valid_from'] is not None and right['valid_until'] <= left['valid_from']):
            continue
        pairs.append((left['observation_id'], right['observation_id']))
    return pairs


def fact_values(snapshot, ontology):
    if len(snapshot.chunks) != len(snapshot.extractions):
        raise ValueError('incomplete_fact_extraction')
    result = []
    for chunk, value in zip(snapshot.chunks, snapshot.extractions, strict=True):
        value = validate_extraction(chunk, ontology, value)
        entities = {e.key: e for e in value.entities}
        for relation in value.relations:
            if not relation.explicit:
                continue
            if relation.valid_from is not None and relation.valid_until is not None and relation.valid_until <= relation.valid_from:
                raise ValueError('fact_validity_interval_empty')
            subject, obj = entities[relation.subject], entities[relation.object]
            qualifier = canonical([subject.qualifier,obj.qualifier])
            values = [subject.name,subject.type,
                      entity_id(snapshot.company_id,snapshot.collection_id,subject.type,subject.name,subject.qualifier),
                      entity_id(snapshot.company_id,snapshot.collection_id,obj.type,obj.name,obj.qualifier),
                      relation.type,obj.name,obj.type,qualifier,
                      relation.valid_from,relation.valid_until]
            identity = [snapshot.company_id,snapshot.collection_id,*[normalized_name(x) if isinstance(x,str)
                        else x.astimezone(timezone.utc).isoformat() if x is not None else None for x in values]]
            result.append(('fact-'+digest(canonical(identity)), values, chunk, relation.quote))
    return result


class FactRecorder:
    def __init__(self, store):
        self.store = store

    def correct(self, company_id, old_id, new_id, *, event_id):
        """Trusted administrative operation, never exposed as a read/LLM tool.

        New uploads only add observations. Only an explicit audited correction
        closes system knowledge; ACL/projection deletion never calls this method.
        """
        if not isinstance(event_id,str) or not 1<=len(event_id)<=128 or old_id == new_id:
            raise ValueError('invalid_fact_correction')
        payload = digest(canonical([company_id,old_id,new_id]))
        key = 'correct-'+digest(canonical([company_id,event_id]))
        with self.store.connect() as c:
            rows = c.execute('SELECT * FROM sk_fact_observations WHERE company_id=%s AND id=ANY(%s::text[]) '
                             'ORDER BY id FOR UPDATE',(company_id,[old_id,new_id])).fetchall()
            if len(rows)!=2:
                raise ValueError('correction_facts_unavailable')
            old,new = ({r['id']:r for r in rows}[i] for i in (old_id,new_id))
            prior = c.execute('SELECT payload_hash FROM sk_fact_events WHERE event_key=%s',(key,)).fetchone()
            if prior:
                if prior['payload_hash'] != payload:
                    raise ValueError('fact_event_idempotency_conflict')
                return True
            if old['recorded_until'] is not None or new['recorded_until'] is not None or new['supersedes'] is not None:
                raise ValueError('fact_correction_conflict')
            if any(old[k] != new[k] for k in ('collection_id','subject_id','relation')):
                raise ValueError('fact_correction_scope_mismatch')
            support = c.execute('SELECT v.document_id,fs.version_id FROM sk_fact_supports fs '
                                'JOIN sk_versions v ON v.id=fs.version_id WHERE fs.observation_id=%s '
                                'AND fs.recorded_until IS NULL ORDER BY fs.version_id LIMIT 1',(new_id,)).fetchone()
            if support is None:
                raise ValueError('fact_correction_support_missing')
            moment = c.execute('SELECT clock_timestamp() t').fetchone()['t']
            c.execute("INSERT INTO sk_fact_events VALUES(%s,%s,%s,%s,'correct',%s,%s)",
                (key,company_id,support['document_id'],support['version_id'],payload,moment))
            c.execute('UPDATE sk_fact_observations SET recorded_until=%s WHERE id=%s',(moment,old_id))
            c.execute('UPDATE sk_fact_observations SET supersedes=%s WHERE id=%s',(old_id,new_id))
            return True

    def record(self, job, snapshot, ontology):
        values = fact_values(snapshot, ontology)
        payload_hash = digest(canonical([v.model_dump(mode='json') for v in snapshot.extractions]))
        event_key = digest(canonical([snapshot.company_id,snapshot.document_id,snapshot.version_id,snapshot.projection_key]))
        with self.store.connect() as c:
            c.execute('SELECT id FROM sk_documents WHERE id=%s FOR UPDATE', (snapshot.document_id,))
            if self.store._graph_current(c, job) is None:
                return False
            if not c.execute("SELECT id FROM sk_graph_jobs WHERE id=%s AND claim_token=%s AND state='working' "
                             'AND lease_until>now() FOR UPDATE', self.store._claimed(job)).fetchone():
                return False
            if (snapshot.document_id != job['document_id'] or snapshot.version_id != job['version_id']
                    or snapshot.company_id != job['company_id'] or snapshot.collection_id != job['collection_id']
                    or snapshot.projection_key != job['projection_key']):
                raise ValueError('fact_job_binding_mismatch')
            old = c.execute('SELECT payload_hash FROM sk_fact_events WHERE event_key=%s', (event_key,)).fetchone()
            if old:
                if old['payload_hash'] != payload_hash:
                    raise ValueError('fact_event_idempotency_conflict')
                return True
            accepted = c.execute('SELECT clock_timestamp() t').fetchone()['t']
            c.execute("INSERT INTO sk_fact_events VALUES(%s,%s,%s,%s,'accept',%s,%s)",
                      (event_key,snapshot.company_id,snapshot.document_id,snapshot.version_id,payload_hash,accepted))
            for fact_id, fields, chunk, quote in values:
                actual = c.execute('SELECT text FROM sk_chunks WHERE version_id=%s AND chunk_id=%s',
                                   (snapshot.version_id,chunk.evidence.chunk_id)).fetchone()
                if actual is None or actual['text'] != chunk.text or quote not in actual['text']:
                    raise ValueError('fact_evidence_binding_mismatch')
                c.execute('INSERT INTO sk_fact_observations(id,company_id,collection_id,subject,subject_type,subject_id,object_id,relation,'
                          'object,object_type,qualifier,valid_from,valid_until,recorded_from) '
                          'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                          (fact_id,snapshot.company_id,snapshot.collection_id,*fields,accepted))
                c.execute('INSERT INTO sk_fact_supports(observation_id,version_id,chunk_id,quote,recorded_from) '
                          'VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                          (fact_id,snapshot.version_id,chunk.evidence.chunk_id,quote,accepted))
            return True
