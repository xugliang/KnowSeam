"""Local, frozen evaluation bundles; no uploads, generated gold labels or writes to sources."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..contracts import RequestModel
from ..config import Identifier
from ..domain import Principal
from ..eval import EvaluationError, evaluate
from .cases import EvaluationCaseV2


class EvaluationManifest(RequestModel):
    principal_ref: Identifier
    snapshot_ref: Identifier
    snapshot_sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    actor_id: Identifier
    company_id: Identifier
    product_id: Identifier
    collection_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    index_revision: Annotated[str, Field(min_length=1, max_length=256)]
    embedding_identity: Annotated[str, Field(min_length=1, max_length=512)]
    reranker_identity: Annotated[str, Field(min_length=1, max_length=512)]
    tokenizer_fingerprint: Annotated[str, Field(min_length=1, max_length=256)]
    candidate_limit: Annotated[int, Field(ge=1, le=100)] = 40
    k: Annotated[int, Field(ge=1, le=20)] = 8
    quality_claim: Literal['contract_only', 'private_unverified'] = 'private_unverified'

    @model_validator(mode='after')
    def budgets(self):
        if self.k > self.candidate_limit or len(set(self.collection_ids)) != len(self.collection_ids):
            raise ValueError('invalid manifest budget or scope')
        return self


@dataclass(frozen=True)
class EvaluationBundle:
    manifest: EvaluationManifest
    cases: tuple[EvaluationCaseV2, ...]
    checksum: str


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError('duplicate bundle field')
        result[key] = value
    return result


def load_bundle(path: str | Path) -> EvaluationBundle:
    with Path(path).open('rb') as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise EvaluationError('evaluation bundle exceeds size budget')
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
        if not isinstance(value, dict) or set(value) != {'manifest', 'cases'}:
            raise ValueError()
        manifest = EvaluationManifest.model_validate(value['manifest'])
        if not isinstance(value['cases'], list) or not 1 <= len(value['cases']) <= 10000:
            raise ValueError()
        cases = tuple(EvaluationCaseV2.from_mapping(case) for case in value['cases'])
        if any(case.principal_ref != manifest.principal_ref or case.snapshot_ref != manifest.snapshot_ref
               or not set(case.collection_ids) <= set(manifest.collection_ids) for case in cases):
            raise ValueError()
    except (ValueError, TypeError, KeyError, RecursionError):
        raise EvaluationError('invalid evaluation bundle') from None
    return EvaluationBundle(manifest, cases, sha256(raw).hexdigest())


async def run_bundle(service, bundle: EvaluationBundle) -> dict:
    m = bundle.manifest
    for model, expected in ((service.embedder, m.embedding_identity),
                            (getattr(service, 'reranker', None), m.reranker_identity)):
        actual = getattr(model, 'cache_identity', getattr(model, 'model_id', None))
        if actual != expected:
            raise EvaluationError('evaluation manifest model mismatch')
    if (service.candidate_limit != m.candidate_limit
            or getattr(service.tokenizer, 'version', None) != m.tokenizer_fingerprint):
        raise EvaluationError('evaluation manifest budget or tokenizer mismatch')
    principal = Principal(m.actor_id, m.company_id, m.product_id, tuple(m.collection_ids))
    report = await evaluate(service, principal, bundle.cases, k=m.k,
                            run_kind='contract' if m.quality_claim == 'contract_only' else 'unverified')
    report['manifest_checksum'] = bundle.checksum
    # Snapshot references freeze the intended experiment, not certify an arbitrary
    # running database. Private gold and snapshot acceptance remain human reviewed.
    report['snapshot_verified'] = False
    return report
