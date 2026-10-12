from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


def test_snapshot_unknown_and_empty_grants_are_distinct():
    from knowseam.permissions.domain import PermissionSnapshot
    empty = PermissionSnapshot(external_id='doc', state='verified', complete=True, public=False, grants=())
    assert empty.grants == ()
    assert PermissionSnapshot(external_id='doc').grants is None
    with pytest.raises(ValidationError):
        PermissionSnapshot(external_id='doc', state='verified', complete=True, public=True, grants=None)


def test_binding_expiry_requires_timezone():
    from knowseam.permissions.domain import SubjectBinding
    with pytest.raises(ValidationError):
        SubjectBinding(actor_id='a', kind='user', subject_id='u', valid_until=datetime(2026, 1, 1))
    assert SubjectBinding(actor_id='a', kind='group', subject_id='g',
                          valid_until=datetime(2027, 1, 1, tzinfo=timezone.utc)).kind == 'group'
