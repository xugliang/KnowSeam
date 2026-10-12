"""Past evidence uses today's ACL and the source configuration of acceptance."""
from datetime import datetime, timezone

from ..domain import NotFound
from .domain import HistoryReader, TemporalUnavailable


def write_metadata(c, version_id, doc_id, node):
    c.execute('INSERT INTO sk_version_metadata(version_id,source_id,config_hash,title,source_url) '
              'SELECT %s,s.id,s.config_hash,%s,%s FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id WHERE d.id=%s',
              (version_id,node.title,node.source_url,doc_id))


def require_history(store, c, principal, document_id, version_id, known_as_of):
    acl, args = store._acl(principal, None)
    row = c.execute('SELECT v.id,m.title,m.source_url,m.accepted_at,m.config_hash AS accepted_config,'
                    's.config_hash,d.active_version,k.id AS collection_id '
                    'FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id '
                    'JOIN sk_collections k ON k.id=s.collection_id JOIN sk_versions v ON v.document_id=d.id '
                    'LEFT JOIN sk_version_metadata m ON m.version_id=v.id WHERE '+acl+' AND d.id=%s AND v.id=%s',
                    (*args,document_id,version_id)).fetchone()
    if row is None or (row['accepted_config'] is not None and row['accepted_config'] != row['config_hash']):
        raise NotFound('historical_document_not_available')
    if row['accepted_at'] is None:
        if known_as_of is not None or row['active_version'] != version_id:
            raise TemporalUnavailable('temporal_unverifiable')
    elif known_as_of is not None and row['accepted_at'] > known_as_of:
        raise TemporalUnavailable('version_not_known_at_time')
    return row


class PostgresHistory(HistoryReader):
    def __init__(self, store, ontologies=None):
        self.store = store
        self.ontologies = ontologies or {}

    def read_history(self, principal, request):
        with self.store.connect() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            doc = require_history(self.store,c,principal,request.document_id,request.version_id,request.known_as_of)
            sections = c.execute('SELECT chunk_id,ordinal,text,heading,locator FROM sk_chunks WHERE version_id=%s '
                                 'ORDER BY ordinal OFFSET %s LIMIT %s',
                                 (request.version_id,request.offset,request.limit+1)).fetchall()
        with self.store.connect() as c:
            doc = require_history(self.store,c,principal,request.document_id,request.version_id,request.known_as_of)
        return dict(document_id=request.document_id,version_id=request.version_id,title=doc['title'],
                    source_url=doc['source_url'],accepted_at=doc['accepted_at'].isoformat() if doc['accepted_at'] else None,
                    metadata_status='complete' if doc['accepted_at'] else 'metadata_incomplete',
                    known_as_of=request.known_as_of.isoformat() if request.known_as_of else None,
                    collection_id=doc['collection_id'],sections=sections[:request.limit],
                    next_offset=request.offset+request.limit if len(sections)>request.limit else None)

    def _search_rows(self, principal, request, only_ids=None):
        acl, args = self.store._acl(principal, request.collection_ids)
        known = request.known_as_of or datetime.now(timezone.utc)
        query = request.query.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        with self.store.connect() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            gap = c.execute('SELECT 1 FROM sk_versions v JOIN sk_documents d ON d.id=v.document_id '
                'JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id '
                'LEFT JOIN sk_version_metadata m ON m.version_id=v.id WHERE '+acl+
                ' AND (m.config_hash IS NULL OR m.config_hash=s.config_hash) '
                'AND (m.accepted_at IS NULL OR m.accepted_at<=%s) '
                'AND EXISTS(SELECT 1 FROM sk_chunks ch WHERE ch.version_id=v.id) '
                "AND NOT EXISTS(SELECT 1 FROM sk_fact_events e WHERE e.version_id=v.id AND e.kind='accept' "
                'AND e.company_id=k.company_id AND e.accepted_at<=%s) LIMIT 1',(*args,known,known)).fetchone() is not None
            rows = c.execute('WITH eligible AS MATERIALIZED (SELECT f.id AS observation_id,f.subject,f.subject_type,f.subject_id,f.object_id,f.relation,f.object,f.object_type,'
                'f.qualifier,f.valid_from,f.valid_until,fs.recorded_from,f.recorded_until,d.id AS document_id,'
                'fs.version_id,fs.chunk_id,fs.quote,m.title,m.source_url,ch.locator,k.id AS collection_id '
                'FROM sk_fact_observations f JOIN sk_fact_supports fs ON fs.observation_id=f.id '
                'JOIN sk_versions v ON v.id=fs.version_id JOIN sk_documents d ON d.id=v.document_id '
                'JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id '
                'JOIN sk_version_metadata m ON m.version_id=v.id JOIN sk_chunks ch ON ch.version_id=v.id AND ch.chunk_id=fs.chunk_id '
                'WHERE '+acl+' AND f.company_id=k.company_id AND f.collection_id=k.id AND m.source_id=s.id '
                'AND (%s::text[] IS NULL OR f.id=ANY(%s::text[])) '
                'AND m.config_hash=s.config_hash AND m.accepted_at<=%s '
                'AND f.recorded_from<=%s AND (f.recorded_until IS NULL OR f.recorded_until>%s) '
                'AND fs.recorded_from<=%s AND (fs.recorded_until IS NULL OR fs.recorded_until>%s) '
                'AND ((f.valid_from IS NOT NULL OR f.valid_until IS NOT NULL) OR %s) '
                'AND (f.valid_from IS NULL OR f.valid_from<=%s) AND (f.valid_until IS NULL OR f.valid_until>%s) '
                "AND concat_ws(' ',f.subject,f.relation,f.object,fs.quote) ILIKE %s "
                '), selected AS (SELECT DISTINCT observation_id FROM eligible ORDER BY observation_id LIMIT %s), '
                'ranked AS (SELECT e.*,row_number() OVER(PARTITION BY e.observation_id ORDER BY e.version_id,e.chunk_id) AS support_rank,'
                'count(*) OVER(PARTITION BY e.observation_id) AS support_count,'
                'min(e.recorded_from) OVER(PARTITION BY e.observation_id) AS visible_recorded_from '
                'FROM eligible e JOIN selected USING(observation_id)) SELECT * FROM ranked WHERE support_rank<=3 '
                'ORDER BY observation_id,support_rank',
                (*args,only_ids,only_ids,known,known,known,known,known,request.include_unknown_validity,
                 request.valid_as_of,request.valid_as_of,'%'+query+'%',request.limit+1)).fetchall()
        return rows, gap

    def search_history(self, principal, request):
        candidates, _ = self._search_rows(principal,request)
        # Recompute all support metadata and lifecycle predicates under fresh ACL,
        # not merely the visibility of each previously selected document.
        rows, gap = self._search_rows(principal,request,list({r['observation_id'] for r in candidates}))
        observations = {}
        for row in rows:
            key = row['observation_id']
            citation = {k: row[k] for k in ('document_id','version_id','chunk_id','quote','title','source_url','locator','collection_id')}
            if key not in observations:
                observations[key] = {k: (v.isoformat() if isinstance(v,datetime) else v) for k,v in row.items()
                                     if k not in citation and k not in {'support_rank','support_count','visible_recorded_from'}}
                observations[key]['recorded_from'] = row['visible_recorded_from'].isoformat()
                observations[key]['supports_truncated'] = row['support_count']>3
                observations[key]['validity_status'] = 'unknown' if row['valid_from'] is None and row['valid_until'] is None else 'known'
                observations[key]['citations'] = []
            observations[key]['citations'].append(citation)
        values = list(observations.values())
        from ..graph.facts import conflicts
        conflict_pairs = []
        for collection, ontology in self.ontologies.items():
            scoped = [dict(o,valid_from=datetime.fromisoformat(o['valid_from']) if o['valid_from'] else None,
                           valid_until=datetime.fromisoformat(o['valid_until']) if o['valid_until'] else None)
                      for o in values[:request.limit] if o['citations'][0]['collection_id']==collection]
            conflict_pairs.extend(conflicts(scoped,ontology.single_valued_relations))
        return dict(observations=values[:request.limit],conflicts=conflict_pairs,
                    temporal_status='partial' if gap or len(values)>request.limit or any(o['supports_truncated'] for o in values[:request.limit]) else 'available',
                    fact_coverage='unextracted_versions' if gap else 'accepted_versions_extracted',
                    valid_as_of=request.valid_as_of.isoformat(),known_as_of=request.known_as_of.isoformat() if request.known_as_of else None)
