"""Strict graph opt-in and secret-free projection fingerprints."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .domain import GraphOntology, Identifier


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, validate_default=True)


class ChatConfig(_Strict):
    base_url: Annotated[str, Field(strict=True, min_length=1, max_length=2048)]
    model_id: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    model_revision: Annotated[str, Field(strict=True, max_length=256)] = ""
    api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    allow_online: Annotated[bool, Field(strict=True)] = False
    timeout: Annotated[float, Field(strict=True, gt=0, le=120, allow_inf_nan=False)] = 60
    max_chunk_chars: Annotated[int, Field(strict=True, ge=1, le=16000)] = 8000

    @model_validator(mode="after")
    def explicit_endpoint(self):
        try:
            parsed = urlsplit(self.base_url)
            parsed.port
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment or "\\" in self.base_url
                    or any(char.isspace() or ord(char) < 32 for char in self.base_url)):
                raise ValueError
        except ValueError:
            raise ValueError("graph chat URL must be HTTP(S) without credentials, query or fragment") from None
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback and not self.allow_online:
            raise ValueError("non-loopback graph chat requires explicit allow_online")
        if not self.model_id.strip() or any(ord(char) < 32 for char in self.model_id + self.model_revision):
            raise ValueError("graph model identity must be printable and nonempty")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.api_key.get_secret_value()):
            raise ValueError("graph API key must not contain control characters")
        return self


class GraphConfig(_Strict):
    enabled: Annotated[bool, Field(strict=True)] = False
    extraction_cache: Annotated[bool, Field(strict=True)] = False
    uri: Annotated[str, Field(strict=True, min_length=1, max_length=2048)] = "bolt://neo4j:7687"
    database: Annotated[str, Field(strict=True, min_length=1, max_length=128)] = "neo4j"
    username: Annotated[str, Field(strict=True, min_length=1, max_length=128)] = "neo4j"
    password: SecretStr = Field(default=SecretStr(""), repr=False)
    collections: dict[Identifier, GraphOntology] = Field(default_factory=dict, max_length=1000)
    extractor: ChatConfig | None = None
    hops: Annotated[int, Field(strict=True, ge=1, le=2)] = 1
    candidate_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 40
    include_inferred: Annotated[bool, Field(strict=True)] = False
    min_confidence: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] = 0.7
    poll_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 5
    lease_seconds: Annotated[int, Field(strict=True, ge=60, le=3600)] = 600
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=3)] = 3

    @model_validator(mode="after")
    def opt_in(self):
        try:
            parsed = urlsplit(self.uri)
            parsed.port
            if (parsed.scheme not in {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}
                    or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment or "\\" in self.uri
                    or any(char.isspace() or ord(char) < 32 for char in self.uri)):
                raise ValueError
        except ValueError:
            raise ValueError("graph URI must be a Neo4j URI without embedded credentials") from None
        if not self.database.strip() or not self.username.strip():
            raise ValueError("graph database and username must not be blank")
        if self.enabled and (not self.password.get_secret_value() or not self.collections or self.extractor is None):
            raise ValueError("enabled graph requires password, collection ontology and extractor")
        if self.extraction_cache and (self.extractor is None or not self.extractor.model_revision.strip()):
            raise ValueError('cross_version_extraction_cache_requires_model_revision')
        if self.enabled and self.lease_seconds < 4 * self.extractor.timeout + 30:
            raise ValueError("graph lease must cover bounded extraction attempts and processing overhead")
        return self

    def projection_key(self, collection_id: str) -> str:
        from .extraction import implementation_fingerprint
        ontology = self.collections.get(collection_id)
        if ontology is None or self.extractor is None:
            raise ValueError("graph projection requires a configured collection and extractor")
        values = ontology.model_dump(mode="json")
        values["entity_types"] = sorted(values["entity_types"])
        values["relation_types"] = sorted(values["relation_types"])
        payload = {"ontology": values,
                   "extractor": self.extractor.model_dump(
                       mode="json", include={"base_url", "model_id", "model_revision", "max_chunk_chars"}),
                   "extractor_version": "openai-chat-v1", "implementation": implementation_fingerprint()}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
