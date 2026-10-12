from collections import Counter

from psycopg.types.json import Jsonb

from ..chunking import digest
from .cache import TokenCache, TokenPayload, TokenBatch, payload_key
from .diff import plan_diff

EFFECTIVE_TERMS = ('(SELECT p.version_id,p.chunk_id,t.term,t.tf FROM sk_term_postings p '
    'JOIN sk_term_payloads t ON t.payload_id=p.payload_id '
    'UNION ALL SELECT t.version_id,t.chunk_id,t.term,t.tf FROM sk_chunk_terms t '
    'WHERE NOT EXISTS (SELECT 1 FROM sk_term_postings p WHERE p.version_id=t.version_id AND p.chunk_id=t.chunk_id))')


class PostgresTokenCache(TokenCache):
    def __init__(self, store, *, mode='shadow'):
        if mode not in ('shadow', 'reuse'):
            raise ValueError('invalid index mode')
        self.store, self.mode = store, mode
        self.identity = 'term-payload-v1:' + mode
        self.last_stats = {}
        self.last_diff = None
        with store.connect() as c:
            if c.execute("SELECT to_regclass('sk_token_cache') name").fetchone()['name'] is None:
                raise RuntimeError('index_migration_required')

    def prepare(self, source_id, chunks, tokenizer, *, document_id=None):
        fingerprint = tokenizer.version
        with self.store.connect() as c:
            row = c.execute('SELECT k.company_id FROM sk_sources s JOIN sk_collections k ON k.id=s.collection_id WHERE s.id=%s',
                             (source_id,)).fetchone()
            if row is None:
                raise ValueError('index_source_unavailable')
            company = row['company_id']
            old = c.execute('SELECT ch.embedding_text FROM sk_chunks ch JOIN sk_documents d '
                'ON d.active_version=ch.version_id WHERE d.id=%s AND d.source_id=%s ORDER BY ch.ordinal',
                (document_id, source_id)).fetchall() if document_id is not None else []
            inputs = {payload_key(company, fingerprint, h.embedding_text): h.embedding_text for h in chunks}
            cached = {r['payload_id']: r for r in c.execute('SELECT payload_id,tokens,token_count,input_hash FROM sk_token_cache '
                'WHERE company_id=%s AND tokenizer_fingerprint=%s AND payload_id=ANY(%s::text[])',
                (company, fingerprint, list(inputs))).fetchall()}
        payloads = {}
        for key, text in inputs.items():
            if len(text) > getattr(tokenizer, 'max_chars', 1000000):
                raise ValueError('token_input_limit')
            row = cached.get(key)
            tokens = row['tokens'] if row is not None else tokenizer.fts_text(text).split()
            if (not isinstance(tokens, list) or any(not isinstance(t, str) or not t or any(c.isspace() for c in t) for t in tokens)
                or len(tokens) > getattr(tokenizer, 'max_tokens', 200000)
                or row is not None and (row['token_count'] != len(tokens) or row['input_hash'] != digest(text))):
                raise ValueError('invalid_token_payload')
            payloads[key] = TokenPayload(key, digest(text), tuple(tokens))
        self.last_stats = {'cache_hits': len(cached), 'tokenized_inputs': len(inputs)-len(cached), 'postings': len(chunks)}
        self.last_diff = plan_diff([digest(r['embedding_text']) for r in old],
                                   [digest(h.embedding_text) for h in chunks])
        return TokenBatch(company, fingerprint, self.mode,
                          tuple(payloads[payload_key(company, fingerprint, h.embedding_text)] for h in chunks))


def write_index(connection, version_id, doc_id, chunks, batch):
    company = connection.execute('SELECT k.company_id FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id '
                                 'JOIN sk_collections k ON k.id=s.collection_id WHERE d.id=%s', (doc_id,)).fetchone()
    if not company or company['company_id'] != batch.company_id or len(batch.payloads) != len(chunks):
        raise ValueError('index_batch_scope_mismatch')
    connection.execute('INSERT INTO sk_index_revisions VALUES(%s,%s,%s,%s)', (version_id, batch.fingerprint, batch.mode, True))
    for payload in {p.payload_id: p for p in batch.payloads}.values():
        inserted = connection.execute('INSERT INTO sk_token_cache VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING payload_id',
            (payload.payload_id, batch.company_id, batch.fingerprint, payload.input_hash,
             len(payload.tokens), Jsonb(payload.tokens))).fetchone()
        if inserted:
            with connection.cursor() as cursor:
                cursor.executemany('INSERT INTO sk_term_payloads VALUES(%s,%s,%s)',
                    [(payload.payload_id, term, count) for term, count in Counter(payload.tokens).items()])
        else:
            existing = connection.execute('SELECT tokens FROM sk_token_cache WHERE payload_id=%s', (payload.payload_id,)).fetchone()
            if existing['tokens'] != list(payload.tokens):
                raise ValueError('immutable_token_payload_conflict')
    for chunk, payload in zip(chunks, batch.payloads, strict=True):
        if (payload.input_hash != digest(chunk.embedding_text)
                or payload.payload_id != payload_key(batch.company_id, batch.fingerprint, chunk.embedding_text)):
            raise ValueError('index_payload_identity_mismatch')
        connection.execute('INSERT INTO sk_term_postings VALUES(%s,%s,%s)', (version_id, chunk.chunk_id, payload.payload_id))


def require_compatible_index(store, connection, *, principal=None):
    exists = connection.execute("SELECT to_regclass('sk_index_revisions') name").fetchone()['name'] is not None
    if not exists:
        if store.indexing_enabled:
            raise RuntimeError('index_migration_required')
        return
    acl, args = store._acl(principal, None) if principal else ('s.enabled', ())
    if not store.indexing_enabled and connection.execute('SELECT 1 FROM sk_index_revisions r '
        'JOIN sk_documents d ON d.active_version=r.version_id JOIN sk_sources s ON s.id=d.source_id '
        'JOIN sk_collections k ON k.id=s.collection_id WHERE ' + acl + " AND r.mode='reuse' LIMIT 1", args).fetchone():
        raise RuntimeError('index_legacy_rollback_required')
    if store.indexing_enabled and connection.execute('SELECT 1 FROM sk_chunks ch JOIN sk_index_revisions r USING(version_id) '
        'JOIN sk_documents d ON d.active_version=r.version_id JOIN sk_sources s ON s.id=d.source_id '
        'JOIN sk_collections k ON k.id=s.collection_id LEFT JOIN sk_term_postings p USING(version_id,chunk_id) '
        'WHERE ' + acl + ' AND (NOT r.complete OR p.payload_id IS NULL) LIMIT 1', args).fetchone():
        raise RuntimeError('index_revision_incomplete')


def materialize_legacy(store, company_id):
    """Explicit reversible preparation, one version transaction at a time; no model call."""
    count = 0
    while True:
        with store.connect() as c:
            row = c.execute('SELECT r.version_id FROM sk_index_revisions r '
                'JOIN sk_versions v ON v.id=r.version_id JOIN sk_documents d ON d.id=v.document_id '
                'JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id '
                "WHERE k.company_id=%s AND r.mode='reuse' ORDER BY r.version_id LIMIT 1 FOR UPDATE OF r",
                (company_id,)).fetchone()
            if row is None:
                return count
            version = row['version_id']
            c.execute('INSERT INTO sk_chunk_terms SELECT p.version_id,p.chunk_id,t.term,t.tf FROM sk_term_postings p '
                'JOIN sk_term_payloads t USING(payload_id) WHERE p.version_id=%s ON CONFLICT(version_id,chunk_id,term) '
                'DO UPDATE SET tf=excluded.tf', (version,))
            check = c.execute('SELECT 1 FROM sk_chunks ch WHERE ch.version_id=%s AND ch.token_count <> '
                '(SELECT coalesce(sum(tf),0) FROM sk_chunk_terms t WHERE t.version_id=ch.version_id AND t.chunk_id=ch.chunk_id) LIMIT 1',
                (version,)).fetchone()
            if check:
                raise RuntimeError('legacy_materialization_incomplete')
            count += c.execute('SELECT count(*) n FROM sk_chunks WHERE version_id=%s', (version,)).fetchone()['n']
            c.execute("UPDATE sk_index_revisions SET mode='shadow' WHERE version_id=%s", (version,))
