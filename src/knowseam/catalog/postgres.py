"""Catalog facts originate in trusted config and current published source evidence."""
from dataclasses import asdict
import secrets

from psycopg.types.json import Jsonb

from ..chunking import canonical, digest
from ..contracts import StructurePublisher
from ..capabilities import CapabilityUnavailable
from ..storage import document_id
from .domain import CatalogReader
from .service import CursorCodec, CursorStale, catalog_page


def config_revision(config):
    return digest(canonical(config.model_dump(mode='json')))


def configure_catalog(store, company_id, config):
    revision = config_revision(config)
    scopes = {(s.source_id, s.kind) for s in config.scopes} | {(e.source_id, e.kind) for e in config.entries}
    with store.connect() as c:
        # Remove only this company's declarations, not documents or archived evidence.
        c.execute('DELETE FROM sk_catalog_scopes WHERE source_id IN '
                  '(SELECT s.id FROM sk_sources s JOIN sk_collections k ON k.id=s.collection_id WHERE k.company_id=%s) '
                  'AND revision<>%s', (company_id, revision))
        for source, kind in sorted(scopes):
            row = c.execute('SELECT s.id FROM sk_sources s JOIN sk_collections k ON k.id=s.collection_id '
                            'WHERE s.id=%s AND k.company_id=%s', (source, company_id)).fetchone()
            if not row:
                raise ValueError('catalog_source_not_configured')
            c.execute('INSERT INTO sk_catalog_scopes(source_id,kind,revision) VALUES(%s,%s,%s) '
                      'ON CONFLICT(source_id,kind) DO NOTHING', (source, kind, revision))
    store.catalog_enabled = True


def write_catalog(c, version_id, doc_id, chunks, entries, revision):
    source = c.execute('SELECT s.id,s.collection_id FROM sk_sources s JOIN sk_documents d ON d.source_id=s.id '
                       'WHERE d.id=%s', (doc_id,)).fetchone()
    for entry in entries:
        if entry.source_id != source['id']:
            raise ValueError('catalog_source_mismatch')
        metadata = {**entry.metadata, '_config_revision': revision}
        c.execute('INSERT INTO sk_catalog_items(collection_id,kind,item_id,name,aliases,metadata) VALUES(%s,%s,%s,%s,%s,%s) '
                  'ON CONFLICT(collection_id,kind,item_id) DO UPDATE SET name=excluded.name,aliases=excluded.aliases,metadata=excluded.metadata',
                  (source['collection_id'], entry.kind, entry.item_id, entry.name, Jsonb(entry.aliases), Jsonb(metadata)))
        # The declaration identifies a source document, not a fabricated quote.
        # Keep a bounded current chunk reference that callers can expand/read.
        for chunk in chunks[:20]:
            c.execute('INSERT INTO sk_catalog_evidence VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                      (source['collection_id'], entry.kind, entry.item_id, version_id, chunk.chunk_id))


class CatalogPublisher(StructurePublisher):
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.identity = config_revision(config)
        self.entries = {}
        for entry in config.entries:
            self.entries.setdefault(document_id(entry.source_id, entry.external_id), []).append(entry)

    def publish_structured(self, parsed, **publication):
        return self.store.publish_structured(parsed, **publication,
            _catalog_entries=self.entries.get(publication['doc_id'], ()), _catalog_revision=self.identity)


class PostgresCatalogReader(CatalogReader):
    def __init__(self, store):
        self.store = store
        self.cursor = CursorCodec(secrets.token_bytes(32))

    def _snapshot(self, principal, request):
        acl, args = self.store._acl(principal, request.collection_ids)
        with self.store.connect() as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            rows = c.execute('SELECT i.collection_id,i.kind,i.item_id,i.name,i.aliases,i.metadata, '
                'd.id AS document_id,d.title,d.source_url,v.id AS version_id,ch.chunk_id,ch.locator '
                'FROM sk_catalog_items i JOIN sk_catalog_evidence e USING(collection_id,kind,item_id) '
                'JOIN sk_chunks ch ON ch.version_id=e.version_id AND ch.chunk_id=e.chunk_id '
                'JOIN sk_versions v ON v.id=e.version_id JOIN sk_documents d ON d.id=v.document_id AND d.active_version=v.id '
                'JOIN sk_sources s ON s.id=d.source_id JOIN sk_collections k ON k.id=s.collection_id AND k.id=i.collection_id '
                'JOIN sk_catalog_scopes cs ON cs.source_id=s.id AND cs.kind=i.kind '
                "WHERE " + acl + " AND i.kind=%s AND i.metadata->>'_config_revision'=cs.revision "
                'ORDER BY i.collection_id,i.item_id,d.id,ch.ordinal LIMIT 50001', (*args, request.kind)).fetchall()
            # Only source scopes backed by a currently accessible document are visible.
            scopes = c.execute('SELECT DISTINCT cs.source_id,cs.revision,cs.complete,s.status '
                'FROM sk_catalog_scopes cs JOIN sk_sources s ON s.id=cs.source_id '
                'JOIN sk_collections k ON k.id=s.collection_id JOIN sk_documents d ON d.source_id=s.id '
                'WHERE ' + acl + ' AND cs.kind=%s ORDER BY cs.source_id', (*args, request.kind)).fetchall()
        if len(rows) > 50000:
            raise CapabilityUnavailable('catalog_scope_exceeds_budget')
        by_item = {}
        for row in rows:
            key = (row['collection_id'], row['item_id'])
            item = by_item.setdefault(key, {k: row[k] for k in ('collection_id', 'kind', 'item_id', 'name', 'aliases')})
            item['metadata'] = {k: v for k, v in row['metadata'].items() if not k.startswith('_')}
            evidence = item.setdefault('evidence', [])
            if len(evidence) < 20:
                evidence.append({k: row[k] for k in ('document_id', 'version_id', 'chunk_id', 'title', 'source_url', 'locator')})
        return list(by_item.values()), scopes, digest(canonical({'rows': rows, 'scopes': scopes}))

    def list_catalog(self, principal, request):
        items, scopes, revision = self._snapshot(principal, request)
        scope = digest(canonical({'principal': asdict(principal), 'collections': sorted(request.collection_ids), 'kind': request.kind}))
        offset = self.cursor.decode(request.cursor, scope=scope, revision=revision) if request.cursor else 0
        page = catalog_page(items, declared=bool(scopes), complete=bool(scopes) and all(
            s['complete'] and s['status'] == 'ready' for s in scopes), offset=offset, limit=request.limit)
        _, _, current = self._snapshot(principal, request)
        if current != revision:
            raise CursorStale('cursor_stale')
        next_offset = page.pop('next_offset')
        page['next_cursor'] = self.cursor.encode(scope=scope, revision=revision, offset=next_offset) if next_offset is not None else None
        return page
