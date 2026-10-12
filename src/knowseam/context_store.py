"""PostgreSQL structure publication and current-version context authorization."""
from psycopg.types.json import Jsonb

from .context import assemble_context
from .contracts import ContextReader, ContextRequest
from .domain import NotFound


def write_structure(connection, version_id, parsed, chunks):
    for ordinal, section in enumerate(parsed.sections):
        connection.execute('INSERT INTO sk_sections(version_id,section_id,ordinal,text,heading,locator) '
                           'VALUES(%s,%s,%s,%s,%s,%s)',
                           (version_id, f's-{ordinal}', ordinal, section.text, section.heading, Jsonb(section.locator)))
    lengths = {f's-{i}': len(s.text) for i, s in enumerate(parsed.sections)}
    for chunk in chunks:
        if not chunk.spans:
            raise ValueError('missing_original_span')
        for span in chunk.spans:
            if span.section_id not in lengths or not 0 <= span.start_char < span.end_char <= lengths[span.section_id]:
                raise ValueError('invalid_original_span')
            connection.execute('INSERT INTO sk_chunk_spans VALUES(%s,%s,%s,%s,%s)',
                               (version_id, chunk.chunk_id, span.section_id, span.start_char, span.end_char))


def require_document(store, connection, principal, document_id, version_id):
    acl, args = store._acl(principal, None)
    doc = connection.execute('SELECT d.title,d.source_url,k.id AS collection_id FROM sk_documents d '
                             'JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id '
                             'WHERE ' + acl + ' AND d.id=%s AND d.active_version=%s',
                             (*args, document_id, version_id)).fetchone()
    if doc is None:
        raise NotFound('document_not_available')
    return doc


class PostgresContextReader(ContextReader):
    def __init__(self, store):
        self.store = store

    def read_context(self, principal, request: ContextRequest):
        with self.store.connect() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            doc = require_document(self.store, c, principal, request.document_id, request.version_id)
            anchors = c.execute('SELECT ordinal,chunk_id FROM sk_chunks WHERE version_id=%s AND chunk_id=ANY(%s::text[])',
                                (request.version_id, request.chunk_ids)).fetchall()
            if {r['chunk_id'] for r in anchors} != set(request.chunk_ids):
                raise NotFound('context_not_available')
            ordinals = sorted({n for row in anchors for n in range(
                max(0, row['ordinal'] - (request.before if request.mode == 'neighbors' else 0)),
                row['ordinal'] + (request.after if request.mode == 'neighbors' else 0) + 1)})
            chunks = c.execute('SELECT chunk_id,ordinal,left(text,%s) AS text,locator FROM sk_chunks '
                               'WHERE version_id=%s AND ordinal=ANY(%s::int[]) ORDER BY ordinal LIMIT 221',
                               (request.max_chars+1, request.version_id, ordinals)).fetchall()
            spans = c.execute('SELECT chunk_id,section_id,start_char,end_char FROM sk_chunk_spans '
                              'WHERE version_id=%s AND chunk_id=ANY(%s::text[])',
                              (request.version_id, [row['chunk_id'] for row in chunks])).fetchall()
            # Parents may be longer than the return budget. Neighbor spans can be
            # far into a section, so retain the original text for exact slicing;
            # ingestion already enforces the parser's total document-size bound.
            sections = c.execute('SELECT section_id,ordinal,text,heading,locator FROM sk_sections '
                                 'WHERE version_id=%s AND section_id=ANY(%s::text[]) ORDER BY ordinal',
                                 (request.version_id, list({s['section_id'] for s in spans}))).fetchall()
        result = assemble_context(request, chunks, sections, spans)
        # New transaction: revocation or publication during assembly is not hidden
        # by the repeatable-read snapshot used above.
        with self.store.connect() as c:
            doc = require_document(self.store, c, principal, request.document_id, request.version_id)
        result['citations'] = [{**citation, **doc} for citation in result['citations']]
        result['collection_id'] = doc['collection_id']
        return result
