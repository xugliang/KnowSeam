"""Bounded visual contracts; separate from text embedding and search hits."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..chunking import canonical, digest
from ..identifiers import Identifier


class RequestModel(BaseModel):
    # Dependency-leaf model: configuration imports this module too.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


Hash = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class VisualConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    enabled: bool = False
    allow_online: Literal[False] = False
    model_id: Literal["vidore/colqwen2-v1.0"] = "vidore/colqwen2-v1.0"
    model_revision: str = ""
    model_sha256: str = ""
    processor_sha256: str = ""
    dependency_sha256: str = ""
    model_path: Path | None = None
    socket_path: Path = Path("/run/knowseam/visual.sock")
    device: str = "cpu"
    dtype: Literal["float32", "bfloat16", "float16"] = "float32"
    query_prefix: Literal["Query: "] = "Query: "
    dimensions: Annotated[int, Field(strict=True, ge=2, le=4096)] = 128
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=600)] = 120
    lease_seconds: Annotated[int, Field(strict=True, ge=60, le=3600)] = 900
    candidate_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 40
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=3)] = 3

    @model_validator(mode="after")
    def configured(self):
        if self.enabled:
            import re

            if not self.model_revision or any(
                not re.fullmatch("[0-9a-f]{64}", v)
                for v in (self.model_sha256, self.processor_sha256, self.dependency_sha256)
            ):
                raise ValueError("visual_pinned_fingerprints_required")
            if self.dimensions != 128 or self.lease_seconds < self.timeout_seconds + 30:
                raise ValueError("visual_runtime_limits")
        return self

    @property
    def fingerprint(self):
        return digest(
            canonical(
                {
                    k: getattr(self, k)
                    for k in (
                        "model_id",
                        "model_revision",
                        "model_sha256",
                        "processor_sha256",
                        "dependency_sha256",
                        "query_prefix",
                        "dimensions",
                        "dtype",
                    )
                }
            )
        )


class AssetRequest(RequestModel):
    """A renderer request bound to already archived bytes; never a filesystem path."""

    source_sha256: Hash
    media_type: Literal["application/pdf", "image/png", "image/jpeg", "image/tiff"]
    page: Annotated[int, Field(ge=1, le=500)] | None = None
    kind: Literal["page", "image", "table", "figure"] = "page"
    bbox: Annotated[list[float], Field(min_length=4, max_length=4)] | None = None
    table_id: Identifier | None = None

    @model_validator(mode="after")
    def region(self):
        if self.bbox is not None:
            x, y, u, v = self.bbox
            if not all(math.isfinite(n) and 0 <= n <= 1 for n in self.bbox) or x >= u or y >= v:
                raise ValueError("invalid_visual_region")
        return self


class MultiVector(RequestModel):
    dimensions: Annotated[int, Field(ge=2, le=4096)]
    vectors: Annotated[list[list[float]], Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def shape(self):
        if self.dimensions * len(self.vectors) > 262144:
            raise ValueError("multivector_payload_limit")
        for row in self.vectors:
            if len(row) != self.dimensions or not all(math.isfinite(x) for x in row) or not any(row):
                raise ValueError("invalid_multivector")
            if not math.isfinite(math.hypot(*row)):
                raise ValueError("invalid_multivector_norm")
        return self

    def normalized(self):
        return [[x / size for x in row] for row in self.vectors for size in [math.hypot(*row)]]

    def coarse(self):
        rows = self.normalized()
        mean = [sum(row[i] for row in rows) / len(rows) for i in range(self.dimensions)]
        size = math.sqrt(sum(x * x for x in mean))
        # Opposing vectors can cancel; a stable nonzero first token remains a valid coarse approximation.
        return [x / size for x in mean] if size > 1e-12 else rows[0]


def maxsim(query, document):
    if query.dimensions != document.dimensions:
        raise ValueError("visual_dimension_mismatch")
    q, d = query.normalized(), document.normalized()
    try:
        import numpy as np
    except ImportError:
        return sum(max(sum(x * y for x, y in zip(a, b)) for b in d) for a in q)
    return float((np.asarray(q, dtype=np.float32) @ np.asarray(d, dtype=np.float32).T).max(axis=1).sum())


@dataclass(frozen=True)
class VisualAsset:
    data: bytes
    width: int
    height: int
    page: int
    kind: str
    bbox: list | None = None
    table_id: str | None = None

    @property
    def sha256(self):
        return digest(self.data)

    @property
    def locator(self):
        return dict(page=self.page, kind=self.kind, bbox=self.bbox, table_id=self.table_id)


class VisualEncoder(ABC):
    fingerprint: str

    @abstractmethod
    async def encode_queries(self, texts) -> list[MultiVector]: ...
    @abstractmethod
    async def encode_assets(self, assets) -> list[MultiVector]: ...
    async def close(self):
        pass

    async def ready(self):
        return False


class VisualSearchRequest(RequestModel):
    query: Annotated[str, Field(min_length=1, max_length=4096)]
    collection_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)] | None = None
    limit: Annotated[int, Field(ge=1, le=20)] = 8


class VisualRetrievalBackend(ABC):
    @abstractmethod
    def candidates(self, principal, collections, vector, limit): ...
    @abstractmethod
    def revalidate(self, principal, hits): ...
    @abstractmethod
    def read_asset(self, principal, asset_id, version_id): ...
