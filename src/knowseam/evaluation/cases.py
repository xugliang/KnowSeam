from dataclasses import dataclass
from collections.abc import Mapping

from pydantic import ValidationError

from ..eval import EvaluationCase, EvaluationError
from ..evidence import EvidenceGroup

CASE_TYPES = frozenset({"fact", "enumeration", "cross_section", "multi_document", "conflict",
                        "noanswer", "terminology", "table", "permission", "incremental"})


@dataclass(frozen=True)
class EvaluationCaseV2(EvaluationCase):
    case_type: str = "fact"
    principal_ref: str = ""
    snapshot_ref: str = ""
    required_evidence_groups: tuple[EvidenceGroup, ...] = ()

    def __post_init__(self):
        super().__post_init__()
        if self.case_type not in CASE_TYPES or any(not isinstance(value, str) or not 1 <= len(value) <= 256
                                                 for value in (self.principal_ref, self.snapshot_ref)):
            raise EvaluationError("v2 case requires type and bounded identity/snapshot references")
        if not isinstance(self.required_evidence_groups, (tuple, list)) or len(self.required_evidence_groups) > 100:
            raise EvaluationError("evidence groups must be a bounded sequence")
        try:
            groups = tuple(EvidenceGroup.model_validate(group) for group in self.required_evidence_groups)
        except ValidationError:
            raise EvaluationError("invalid evidence group") from None
        object.__setattr__(self, "required_evidence_groups", groups)

    @classmethod
    def from_mapping(cls, value: Mapping):
        allowed = {"query", "expected_document_ids", "collection_ids", "case_type", "principal_ref",
                   "snapshot_ref", "required_evidence_groups"}
        if set(value) - allowed or not {"query", "expected_document_ids"} <= set(value):
            raise EvaluationError("invalid v2 case fields")
        return cls(**value)
