from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..chunking import canonical, digest


class IndexingConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    enabled: Annotated[bool, Field(strict=True)] = False
    mode: Literal['shadow', 'reuse'] = 'shadow'


def payload_key(company, fingerprint, text):
    return digest(canonical([company, fingerprint, digest(text)]))


@dataclass(frozen=True)
class TokenPayload:
    payload_id: str
    input_hash: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class TokenBatch:
    company_id: str
    fingerprint: str
    mode: str
    payloads: tuple[TokenPayload, ...]


class TokenCache(ABC):
    @abstractmethod
    def prepare(self, source_id, chunks, tokenizer, *, document_id=None) -> TokenBatch: ...
