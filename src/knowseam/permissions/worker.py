import asyncio
import logging

from ..domain import AccessDenied
from .domain import PermissionSnapshot, PermissionConnector

LOGGER = logging.getLogger('knowseam.permissions')


class PermissionWorker:
    def __init__(self, authority):
        self.authority = authority

    async def refresh(self, source_id, connector, external_ids, *, expected_config_hash=None):
        if not isinstance(connector, PermissionConnector):
            raise ValueError('source_permission_connector_unavailable')
        obs = await asyncio.to_thread(self.authority.begin, source_id, expected_config_hash=expected_config_hash)
        try:
            values = await connector.fetch_permissions(tuple(external_ids))
            snapshots = {v.external_id: PermissionSnapshot.model_validate(v) for v in values}
            if len(snapshots) != len(values) or not set(snapshots).issubset(external_ids):
                raise ValueError('permission_response_scope_mismatch')
        except Exception as error:
            LOGGER.warning('permission_fetch_failed category=%s', type(error).__name__)
            snapshots = {}
        applied = 0
        for external_id in external_ids:
            value = snapshots.get(external_id, PermissionSnapshot(external_id=external_id, reason='unavailable'))
            applied += bool(await asyncio.to_thread(self.authority.apply, obs, value))
        return applied

    async def sync(self, source_id, connector, expected_config_hash):
        def page(after):
            with self.authority.store.connect() as c:
                source = c.execute('SELECT config_hash,enabled FROM sk_sources WHERE id=%s', (source_id,)).fetchone()
                if not source or not source['enabled'] or source['config_hash'] != expected_config_hash:
                    raise AccessDenied('permission_worker_source_changed')
                return [r['external_id'] for r in c.execute("SELECT external_id FROM sk_documents WHERE source_id=%s "
                    "AND external_id>%s AND status!='deleted' ORDER BY external_id LIMIT 100", (source_id, after)).fetchall()]
        after, count = '', 0
        while ids := await asyncio.to_thread(page, after):
            count += await self.refresh(source_id, connector, ids, expected_config_hash=expected_config_hash)
            after = ids[-1]
        return {'source_id': source_id, 'permissions_applied': count}
