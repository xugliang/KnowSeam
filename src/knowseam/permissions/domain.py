from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..identifiers import Identifier
from ..domain import AccessDenied


class PermissionChanged(AccessDenied):
    """Publication lost its authority; do not overwrite a newer permission observation."""


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)


class Grant(Model):
    kind: Literal['user', 'group']
    subject_id: Annotated[str, Field(strict=True, min_length=1, max_length=256)]


class SubjectBinding(Grant):
    actor_id: Identifier
    valid_until: datetime

    @model_validator(mode='after')
    def aware(self):
        if self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None:
            raise ValueError('binding_expiry_requires_timezone')
        return self


class PermissionSnapshot(Model):
    external_id: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
    state: Literal['verified', 'unknown', 'revoked'] = 'unknown'
    public: Annotated[bool, Field(strict=True)] = False
    complete: Annotated[bool, Field(strict=True)] = False
    grants: Annotated[tuple[Grant, ...] | None, Field(max_length=10000)] = None
    reason: Literal['source', 'unavailable', 'unsupported', 'incomplete'] = 'source'

    @model_validator(mode='after')
    def consistent(self):
        if self.state == 'verified' and (not self.complete or self.grants is None):
            raise ValueError('verified_permissions_require_complete_grants')
        if self.public and self.state != 'verified':
            raise ValueError('unverified_permissions_cannot_be_public')
        if self.grants is not None and len({(g.kind, g.subject_id) for g in self.grants}) != len(self.grants):
            raise ValueError('duplicate_source_grants')
        return self


@dataclass(frozen=True)
class Observation:
    source_id: str
    config_hash: str
    sequence: int
    started_at: datetime


class PermissionsConfig(Model):
    enabled: Annotated[bool, Field(strict=True)] = False
    interval_seconds: Annotated[int, Field(strict=True, ge=30, le=3600)] = 300


class PermissionConnector(ABC):
    @abstractmethod
    async def fetch_permissions(self, external_ids) -> tuple[PermissionSnapshot, ...]: ...


def public_snapshot(external_id, public):
    return PermissionSnapshot(external_id=external_id,
        state='verified' if public is True else 'revoked' if public is False else 'unknown',
        complete=public is not None, public=public is True, grants=() if public is not None else None)
