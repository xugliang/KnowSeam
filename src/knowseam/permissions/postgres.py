"""Short permission transactions; no source advisory lock and no model dependency."""
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from ..chunking import canonical, digest
from ..domain import AccessDenied
from .domain import Observation, PermissionSnapshot, PermissionChanged, public_snapshot


class PermissionAuthority:
    def __init__(self, store):
        self.store = store
        with store.connect() as c:
            if c.execute("SELECT to_regclass('sk_document_permissions') name").fetchone()['name'] is None:
                raise RuntimeError('source_acl_migration_required')

    def begin(self, source_id, *, expected_config_hash=None):
        with self.store.connect() as c:
            row = c.execute("SELECT config_hash,nextval('sk_permission_sequence') seq,clock_timestamp() started "
                            "FROM sk_sources WHERE id=%s AND enabled", (source_id,)).fetchone()
            if not row or (expected_config_hash is not None and row['config_hash'] != expected_config_hash):
                raise AccessDenied('permission_source_unavailable')
            return Observation(source_id, row['config_hash'], row['seq'], row['started'])

    def configure(self, c, company, source):
        mode = source.get('permission_mode', 'public_verified')
        if mode not in ('public_verified', 'source_acl'):
            raise ValueError('invalid_source_permission_mode')
        old = c.execute('SELECT * FROM sk_source_policies WHERE source_id=%s', (source['id'],)).fetchone()
        c.execute('INSERT INTO sk_source_policies VALUES(%s,%s,%s) ON CONFLICT(source_id) DO UPDATE '
                  'SET mode=excluded.mode,config_hash=excluded.config_hash',
                  (source['id'], mode, source['config_hash']))
        c.execute('DELETE FROM sk_source_bindings WHERE source_id=%s', (source['id'],))
        from .domain import SubjectBinding
        for raw in source.get('bindings', ()):
            binding = SubjectBinding.model_validate(raw)
            c.execute('INSERT INTO sk_source_bindings VALUES(%s,%s,%s,%s,%s,%s)',
                (source['id'], company, binding.actor_id, binding.kind, binding.subject_id, binding.valid_until))
        if old is None or old['mode'] != mode or old['config_hash'] != source['config_hash']:
            for row in c.execute('SELECT id FROM sk_documents WHERE source_id=%s ORDER BY id FOR UPDATE', (source['id'],)).fetchall():
                self.invalidate(c, row['id'])

    def invalidate(self, c, document_id):
        row = c.execute("SELECT d.source_id,d.external_id,s.config_hash,nextval('sk_permission_sequence') seq,"
                        "clock_timestamp() started FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id "
                        "WHERE d.id=%s FOR UPDATE OF d", (document_id,)).fetchone()
        if row:
            self._apply(c, Observation(row['source_id'], row['config_hash'], row['seq'], row['started']),
                        PermissionSnapshot(external_id=row['external_id'], state='revoked', complete=True, grants=()),
                        document_id, allow_disabled=True)

    def apply(self, observation, snapshot):
        from ..storage import document_id
        snapshot = PermissionSnapshot.model_validate(snapshot)
        with self.store.connect() as c:
            return self._apply(c, observation, snapshot, document_id(observation.source_id, snapshot.external_id))

    def _apply(self, c, obs, snapshot, doc_id, *, allow_disabled=False):
        row = c.execute('SELECT d.*,s.config_hash,s.enabled FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id '
                        'WHERE d.id=%s AND d.source_id=%s FOR UPDATE OF d', (doc_id, obs.source_id)).fetchone()
        if not row or row['config_hash'] != obs.config_hash or (not row['enabled'] and not allow_disabled):
            return False
        old = c.execute('SELECT * FROM sk_document_permissions WHERE document_id=%s', (doc_id,)).fetchone()
        if old and old['observation_sequence'] >= obs.sequence:
            return False
        state = snapshot.state if snapshot.complete else 'unknown'
        public = state == 'verified' and snapshot.public
        verified = obs.started_at if state == 'verified' else None
        grants = sorted((g.kind, g.subject_id) for g in (snapshot.grants or ())) if state == 'verified' else []
        semantics = [obs.config_hash, state, public, snapshot.complete, grants]
        old_grants = sorted((g['kind'], g['subject_id']) for g in c.execute(
            'SELECT kind,subject_id FROM sk_document_grants WHERE document_id=%s', (doc_id,)).fetchall())
        unchanged = old and semantics == [old['config_hash'], old['state'], old['public'], old['complete'], old_grants]
        # A refresh preserves ongoing work, but revoke -> restore is a new generation.
        # A content-only hash would re-authorize work started before revocation (ABA).
        revision = old['revision'] if unchanged else digest(canonical([obs.sequence, semantics]))
        c.execute('INSERT INTO sk_document_permissions VALUES(%s,%s,%s,%s,%s,%s,%s,%s) '
                  'ON CONFLICT(document_id) DO UPDATE SET observation_sequence=excluded.observation_sequence,'
                  'revision=excluded.revision,config_hash=excluded.config_hash,state=excluded.state,'
                  'public=excluded.public,complete=excluded.complete,verified_at=excluded.verified_at',
                  (doc_id, obs.sequence, revision, obs.config_hash, state, public, snapshot.complete, verified))
        c.execute('DELETE FROM sk_document_grants WHERE document_id=%s', (doc_id,))
        if state == 'verified':
            for grant in snapshot.grants:
                c.execute('INSERT INTO sk_document_grants VALUES(%s,%s,%s)', (doc_id, grant.kind, grant.subject_id))
        c.execute("UPDATE sk_documents SET public=%s,acl_verified_at=%s,status=CASE "
                  "WHEN status='deleted' THEN status WHEN %s THEN "
                  "CASE WHEN status='unavailable' AND active_version IS NOT NULL THEN 'stale' ELSE status END "
                  "ELSE 'unavailable' END WHERE id=%s",
                  (public, verified, state == 'verified', doc_id))
        self.store._enqueue_graph(c, doc_id)
        return True

    def observe(self, source_id, node, scanned_at, observation):
        from ..storage import document_id
        if observation is None or observation.source_id != source_id:
            raise AccessDenied('permission_observation_required_before_scan')
        doc_id = document_id(source_id, node.external_id)
        with self.store.connect() as c:
            source = c.execute('SELECT config_hash,enabled FROM sk_sources WHERE id=%s', (source_id,)).fetchone()
            if not source or not source['enabled'] or source['config_hash'] != observation.config_hash:
                raise AccessDenied('permission_source_changed')
            c.execute('INSERT INTO sk_documents(id,source_id,external_id,title,source_url,last_seen,metadata) '
                      'VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET '
                      'title=excluded.title,source_url=excluded.source_url,last_seen=excluded.last_seen,metadata=excluded.metadata',
                      (doc_id, source_id, node.external_id, node.title, node.source_url, scanned_at, Jsonb(node.metadata)))
            self._apply(c, observation, public_snapshot(node.external_id, node.public), doc_id)
            return c.execute('SELECT d.*,v.pipeline_id,v.content_hash,v.raw_hash,p.revision AS permission_revision '
                             'FROM sk_documents d LEFT JOIN sk_versions v ON v.id=d.active_version '
                             'LEFT JOIN sk_document_permissions p ON p.document_id=d.id WHERE d.id=%s', (doc_id,)).fetchone()

    def admission(self, c, doc_id, expected_revision=None):
        row = c.execute('SELECT p.revision FROM sk_documents d JOIN sk_sources s ON s.id=d.source_id '
            'JOIN sk_source_policies policy ON policy.source_id=s.id '
            'JOIN sk_document_permissions p ON p.document_id=d.id '
            "WHERE d.id=%s AND s.enabled AND p.config_hash=s.config_hash AND policy.config_hash=s.config_hash "
            "AND p.state='verified' AND p.complete AND p.verified_at >= %s "
            "AND (p.public OR (policy.mode='source_acl' AND EXISTS "
            '(SELECT 1 FROM sk_document_grants g WHERE g.document_id=d.id)))',
            (doc_id, datetime.now(timezone.utc)-timedelta(seconds=self.store.acl_ttl_seconds))).fetchone()
        return row['revision'] if row and (expected_revision is None or row['revision'] == expected_revision) else None

    def require(self, c, doc_id, expected_revision):
        if expected_revision is None or self.admission(c, doc_id, expected_revision) is None:
            raise PermissionChanged('permission_changed_before_publication')

    def acl(self, principal):
        return (
            "EXISTS (SELECT 1 FROM sk_document_permissions sp JOIN sk_source_policies policy ON policy.source_id=s.id "
            "WHERE sp.document_id=d.id AND sp.config_hash=s.config_hash AND policy.config_hash=s.config_hash "
            "AND sp.state='verified' AND sp.complete AND sp.verified_at >= %s AND (sp.public OR "
            "(policy.mode='source_acl' AND EXISTS (SELECT 1 FROM sk_document_grants g "
            "JOIN sk_source_bindings b ON b.source_id=s.id AND b.kind=g.kind AND b.subject_id=g.subject_id "
            "JOIN sk_employees emp ON emp.company_id=b.company_id AND emp.actor_id=b.actor_id "
            "WHERE g.document_id=d.id AND b.company_id=k.company_id AND b.actor_id=%s "
            "AND emp.active AND b.valid_until>now()))))",
            (datetime.now(timezone.utc)-timedelta(seconds=self.store.acl_ttl_seconds), principal.actor_id))
