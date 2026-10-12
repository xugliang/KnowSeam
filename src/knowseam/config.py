"""Strict JSON-only configuration; no environment expansion or secret logging."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from .document_parsing.config import ParsingConfig
from .graph.config import GraphConfig
from .catalog.domain import CatalogConfig
from .identifiers import ID_PATTERN, Identifier
from .coverage import CoverageConfig
from .indexing.cache import IndexingConfig
from .permissions.domain import PermissionsConfig, SubjectBinding
from .visual.domain import VisualConfig

MAX_CONFIG_BYTES = 1024 * 1024


class ConfigError(ValueError):
    """Safe configuration failure without input values or credentials."""


def _identifier(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(ID_PATTERN, value) is not None


@dataclass(frozen=True)
class ServiceClient:
    """A server-to-server credential bound to one product and company."""

    token: str = field(repr=False)
    product_id: str
    company_id: str
    allowed_collections: tuple[str, ...]
    actor_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.token, str) or not 32 <= len(self.token) <= 512
            or any(ord(character) < 33 or ord(character) > 126 for character in self.token)
        ):
            raise ConfigError("service token must contain 32..512 printable non-space ASCII characters")
        if not _identifier(self.product_id) or not _identifier(self.company_id):
            raise ConfigError("service client must have valid product and company identifiers")
        for name in ("allowed_collections", "actor_ids"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or not all(_identifier(value) for value in values):
                raise ConfigError("service client scopes must contain valid identifiers")
            if len(values) > 10000 or len(set(values)) != len(values):
                raise ConfigError("service client scopes exceed bounds or contain duplicates")
            object.__setattr__(self, name, tuple(values))
        if not self.allowed_collections:
            raise ConfigError("service client must have explicitly authorized collections")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, validate_default=True)


class CollectionConfig(_Strict):
    id: Identifier
    title: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    visibility: Literal["company", "department", "employee"] = "company"
    departments: Annotated[tuple[Identifier, ...], Field(max_length=10000)] = ()
    employees: Annotated[tuple[Identifier, ...], Field(max_length=10000)] = ()

    @model_validator(mode="after")
    def validate_grants(self):
        if ((self.visibility != "department" and self.departments)
                or (self.visibility != "employee" and self.employees)):
            raise ValueError("grants must match collection visibility")
        return self

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("collection title must not be blank")
        return value


class EmployeeConfig(_Strict):
    actor_id: Identifier
    active: Annotated[bool, Field(strict=True)] = True
    departments: Annotated[tuple[Identifier, ...], Field(max_length=1000)] = ()


class ModelConfig(_Strict):
    backend: Literal["qwen", "http"]
    model_id: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    model_revision: Annotated[str, Field(strict=True, max_length=256)] = ""
    model_path: Path | None = None
    base_url: Annotated[str, Field(strict=True, min_length=1, max_length=2048)] | None = None
    api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    dimensions: Annotated[int, Field(strict=True, ge=32, le=4096)] = 1024
    device: Annotated[str, Field(strict=True, min_length=1, max_length=64)] = "cpu"
    dtype: Literal["float32", "float16", "bfloat16"] = "float32"
    max_length: Annotated[int, Field(strict=True, ge=64, le=32768)] = 4096
    batch_size: Annotated[int, Field(strict=True, ge=1, le=64)] = 8
    timeout: Annotated[float, Field(strict=True, gt=0, le=600, allow_inf_nan=False)] = 120
    allow_online: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def correct_backend(self):
        if not self.model_id.strip() or not self.device.strip():
            raise ValueError("model and device names must not be blank")
        if self.backend == "qwen":
            if self.model_path is None or self.base_url is not None or self.api_key.get_secret_value():
                raise ValueError("local Qwen needs model_path and must not have HTTP credentials")
            if self.allow_online:
                raise ValueError("local Qwen does not enable remote model access")
        else:
            if self.base_url is None or self.model_path is not None:
                raise ValueError("HTTP model needs base_url and must not have model_path")
            _http_url(self.base_url)
            import ipaddress
            hostname = urlsplit(self.base_url).hostname
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = hostname == "localhost"
            if not loopback and not self.allow_online:
                raise ValueError("non-loopback model access requires explicit allow_online")
        return self


def _http_url(value: str) -> None:
    parsed = urlsplit(value)
    parsed.port
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(character.isspace() for character in value) or "\\" in value):
        raise ValueError("model URL must be HTTP(S) without credentials, query or fragment")


class ModelsConfig(_Strict):
    embedding: ModelConfig
    reranker: ModelConfig


class EnterpriseTerm(_Strict):
    """A company-owned canonical name and its explicitly approved aliases."""

    canonical: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    aliases: Annotated[tuple[Annotated[str, Field(strict=True, min_length=1, max_length=256)], ...],
                       Field(max_length=32)] = ()
    category: Literal["product", "feature", "field", "organization", "other"] = "other"

    @model_validator(mode="after")
    def validate_names(self):
        names = (self.canonical, *self.aliases)
        normalized = [unicodedata.normalize("NFKC", name).strip().lower() for name in names]
        if (any(not normal or any(unicodedata.category(char).startswith("C") for char in name)
                for name, normal in zip(names, normalized, strict=True)) or len(normalized) != len(set(normalized))):
            raise ValueError("enterprise terminology contains blank, control or duplicate names")
        return self




class TokenizationConfig(_Strict):
    """Deployment vocabulary shared by indexing and querying, never product defaults."""

    terms: Annotated[tuple[Annotated[str, Field(strict=True, min_length=1, max_length=256)], ...],
                     Field(max_length=100000)] = ()
    glossary: Annotated[tuple[EnterpriseTerm, ...], Field(max_length=10000)] = ()
    user_dictionary: Path | None = None

    @field_validator("glossary", mode="before")
    @classmethod
    def explicit_glossary_list(cls, values):
        if not isinstance(values, (list, tuple)):
            raise ValueError("tokenization glossary must be a list")
        return values

    @model_validator(mode="after")
    def unique_glossary_names(self):
        names: dict[str, str] = {}
        for entry in self.glossary:
            canonical = unicodedata.normalize("NFKC", entry.canonical).strip().lower()
            for value in (entry.canonical, *entry.aliases):
                name = unicodedata.normalize("NFKC", value).strip().lower()
                if name in names and names[name] != canonical:
                    raise ValueError("enterprise terminology maps one name to multiple canonical names")
                names[name] = canonical
        return self


    @field_validator("terms", mode="before")
    @classmethod
    def explicit_term_list(cls, values):
        if not isinstance(values, (list, tuple)):
            raise ValueError("tokenization terms must be a list")
        return values

    @field_validator("terms")
    @classmethod
    def valid_terms(cls, values):
        for value in values:
            normalized = unicodedata.normalize("NFKC", value).strip()
            if not normalized or any(unicodedata.category(character).startswith("C") for character in value):
                raise ValueError("tokenization terms must be nonblank words without control characters")
        return values

    @field_validator("user_dictionary", mode="before")
    @classmethod
    def explicit_dictionary_path(cls, value):
        if value is not None and (
            not isinstance(value, (str, Path)) or not str(value).strip()
            or "\x00" in str(value) or str(value).startswith("~")
        ):
            raise ValueError("user_dictionary must be an explicit file path")
        return value


class FilesystemSourceConfig(_Strict):
    type: Literal["filesystem"]
    id: Identifier
    collection_id: Identifier
    root: Path
    public_files: tuple[str, ...] = ()
    permission_mode: Literal['public_verified'] = 'public_verified'
    max_download_bytes: Annotated[int, Field(strict=True, ge=1, le=100 * 1024 * 1024)] = 30 * 1024 * 1024

    @field_validator("public_files")
    @classmethod
    def relative_files_only(cls, values):
        if len(values) > 10000 or len(set(values)) != len(values):
            raise ValueError("public file list is duplicated or oversized")
        for value in values:
            windows, posix = PureWindowsPath(value), PurePosixPath(value)
            if (not value or len(value) > 1024 or "\x00" in value or "\\" in value
                    or windows.drive or windows.is_absolute() or posix.is_absolute()
                    or ".." in posix.parts or value in {".", ".."}):
                raise ValueError("public files must be bounded relative paths without traversal")
        return values


class FeishuSourceConfig(_Strict):
    type: Literal["feishu"]
    id: Identifier
    collection_id: Identifier
    permission_mode: Literal['public_verified', 'source_acl'] = 'public_verified'
    bindings: Annotated[tuple['SubjectBinding', ...], Field(max_length=10000)] = ()
    app_id: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    app_secret: SecretStr = Field(repr=False)
    root_token: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_-]{1,200}$")]
    excluded_node_tokens: Annotated[tuple[Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_-]{1,200}$")], ...], Field(max_length=1000)] = ()
    excluded_object_ids: Annotated[tuple[Annotated[str, Field(strict=True, pattern=r"^[a-z0-9_]+:[A-Za-z0-9_-]{1,200}$")], ...], Field(max_length=1000)] = ()
    wiki_origin: str = "https://www.feishu.cn"
    timeout_seconds: Annotated[float, Field(strict=True, gt=0, le=120, allow_inf_nan=False)] = 20
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    max_download_bytes: Annotated[int, Field(strict=True, ge=1, le=100 * 1024 * 1024)] = 30 * 1024 * 1024
    max_nodes: Annotated[int, Field(strict=True, ge=1, le=100000)] = 10000
    max_pages: Annotated[int, Field(strict=True, ge=1, le=10000)] = 1000
    max_sheet_cells: Annotated[int, Field(strict=True, ge=1, le=5000000)] = 2000000

    @model_validator(mode="after")
    def validate_feishu(self):
        if not self.app_id.strip() or not self.app_secret.get_secret_value():
            raise ValueError("Feishu credentials must not be empty")
        origin = urlsplit(self.wiki_origin)
        if (origin.scheme != "https" or not origin.hostname
                or not (origin.hostname == "feishu.cn" or origin.hostname.endswith(".feishu.cn"))
                or origin.username or origin.password or origin.port not in (None, 443)
                or origin.path not in {"", "/"} or origin.query or origin.fragment):
            raise ValueError("wiki_origin must be an official Feishu HTTPS origin")
        return self


SourceConfig = Annotated[FilesystemSourceConfig | FeishuSourceConfig, Field(discriminator="type")]




class ObservabilityConfig(_Strict):
    enabled: Annotated[bool, Field(strict=True)] = False
    diagnostic_actor_ids: Annotated[tuple[Identifier, ...], Field(max_length=10000)] = ()


class FeaturesConfig(_Strict):
    catalog: Annotated[bool, Field(strict=True)] = False
    context: Annotated[bool, Field(strict=True)] = False
    tables: Annotated[bool, Field(strict=True)] = False
    coverage: Annotated[bool, Field(strict=True)] = False
    history: Annotated[bool, Field(strict=True)] = False
    visual: Annotated[bool, Field(strict=True)] = False
    query_graph: Annotated[bool, Field(strict=True)] = False


class Settings(_Strict):
    environment: Literal["production", "development", "test"] = "production"
    dsn: SecretStr = Field(repr=False)
    file_root: Path
    company_id: Identifier
    collections: Annotated[tuple[CollectionConfig, ...], Field(min_length=1, max_length=1000)]
    employees: Annotated[tuple[EmployeeConfig, ...], Field(max_length=10000)] = ()
    clients: Annotated[tuple[ServiceClient, ...], Field(min_length=1, max_length=1000)]
    models: ModelsConfig
    tokenization: TokenizationConfig = Field(default_factory=TokenizationConfig)
    parsing: ParsingConfig = Field(default_factory=ParsingConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    visual: VisualConfig = Field(default_factory=VisualConfig)
    sources: Annotated[tuple[SourceConfig, ...], Field(max_length=1000)] = ()
    sync_interval_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 300
    acl_ttl_seconds: Annotated[int, Field(strict=True, ge=30, le=86400)] = 900

    @field_validator("clients", mode="before")
    @classmethod
    def parse_clients(cls, values):
        if not isinstance(values, (list, tuple)):
            raise ValueError("clients must be a list")
        output = []
        fields = {"token", "product_id", "company_id", "allowed_collections", "actor_ids"}
        for value in values:
            if isinstance(value, ServiceClient):
                output.append(value)
            elif isinstance(value, Mapping) and set(value) <= fields:
                try:
                    output.append(ServiceClient(**value))
                except TypeError:
                    raise ValueError("invalid client configuration") from None
            else:
                raise ValueError("invalid or unknown client configuration field")
        return tuple(output)

    @model_validator(mode="after")
    def validate_relationships(self):
        dsn = self.dsn.get_secret_value()
        parsed_dsn = urlsplit(dsn)
        if (not dsn.startswith(("postgresql://", "postgres://"))
                or parsed_dsn.path in {"", "/"} or parsed_dsn.fragment
                or any(character.isspace() or ord(character) < 32 for character in dsn)):
            raise ValueError("dsn must be a PostgreSQL connection URI")
        collections = {item.id for item in self.collections}
        if self.features.visual != self.visual.enabled:
            raise ValueError('visual_feature_and_runtime_must_be_explicitly_enabled_together')
        if self.features.visual and not self.permissions.enabled:
            raise ValueError('visual_requires_versioned_permission_authority')
        if len(collections) != len(self.collections):
            raise ValueError("collection ids must be unique")
        if not set(self.graph.collections).issubset(collections):
            raise ValueError("graph ontology refers to an unknown collection")
        if self.features.query_graph and (not self.graph.collections or self.graph.extractor is None):
            raise ValueError('query_graph_requires_fact_ontology_and_extractor')
        if ((self.features.history or self.features.query_graph) and self.graph.extractor is not None
                and self.graph.lease_seconds < 4*self.graph.extractor.timeout+30):
            raise ValueError('fact_lease_must_cover_extraction_deadline')
        if len({item.actor_id for item in self.employees}) != len(self.employees):
            raise ValueError("employee ids must be unique")
        if len({item.id for item in self.sources}) != len(self.sources):
            raise ValueError("source ids must be unique")
        if any(source.collection_id not in collections for source in self.sources):
            raise ValueError("source refers to an unknown collection")
        if len({client.token for client in self.clients}) != len(self.clients):
            raise ValueError("service client tokens must be unique")
        for client in self.clients:
            if client.company_id != self.company_id or not set(client.allowed_collections) <= collections:
                raise ValueError("service client exceeds configured company/collection scope")
            if self.environment == "production" and _placeholder(client.token):
                raise ValueError("placeholder service tokens are forbidden in production")
        if self.acl_ttl_seconds < self.sync_interval_seconds:
            raise ValueError("ACL TTL must cover at least one sync interval")
        if self.permissions.enabled and self.permissions.interval_seconds >= self.acl_ttl_seconds:
            raise ValueError('permission interval must be strictly less than ACL TTL')
        if not self.permissions.enabled and any(s.permission_mode != 'public_verified' for s in self.sources):
            raise ValueError('source_acl requires permissions.enabled')
        actors = {e.actor_id for e in self.employees}
        for source in self.sources:
            bindings = getattr(source, 'bindings', ())
            if any(b.actor_id not in actors for b in bindings):
                raise ValueError('source binding requires configured employee')
            if len({(b.actor_id, b.kind, b.subject_id) for b in bindings}) != len(bindings):
                raise ValueError('duplicate source subject binding')
        return self


def _placeholder(token: str) -> bool:
    value = token.lower()
    return (len(set(token)) < 8 or any(marker in value for marker in (
        "changeme", "change-me", "replace-me", "replace_me", "placeholder", "example-token",
        "example_token", "your-token", "your_token", "test-token", "test_token", "dummy-token",
    )))


def _unique_object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ConfigError("duplicate JSON configuration keys are not allowed")
        output[key] = value
    return output


def _reject_constant(value):
    raise ConfigError("non-finite JSON configuration numbers are not allowed")


def _resolve_path(value: object, base: Path, *, write_root: bool = False, regular_file: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or value.startswith("~"):
        raise ConfigError("configuration paths must be explicit nonempty strings")
    candidate = Path(value)
    if candidate.drive and not candidate.is_absolute():
        raise ConfigError("drive-relative configuration paths are not allowed")
    if ".." in candidate.parts:
        raise ConfigError("configuration paths must not contain parent traversal")
    resolved = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if not candidate.is_absolute() and not resolved.is_relative_to(base):
        raise ConfigError("relative configuration path escapes configuration directory")
    if resolved == Path(resolved.anchor) or (write_root and resolved == Path.home().resolve()):
        raise ConfigError("configuration path must name a dedicated directory")
    if regular_file:
        if not resolved.is_file():
            raise ConfigError("user dictionary must name an existing regular file")
        if resolved.stat().st_size > 10 * 1024 * 1024:
            raise ConfigError("user dictionary exceeds the 10 MiB size limit")
    elif resolved.exists() and not resolved.is_dir():
        raise ConfigError("configuration path must be a directory")
    return str(resolved)


def load_config(path: str | Path) -> Settings:
    """Read one bounded JSON file and resolve paths relative to that file only."""
    try:
        filename = Path(path).resolve(strict=True)
        with filename.open("rb") as stream:
            content = stream.read(MAX_CONFIG_BYTES + 1)
        if len(content) > MAX_CONFIG_BYTES:
            raise ConfigError("configuration file exceeds size limit")
        values = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
        if not isinstance(values, dict):
            raise ConfigError("configuration must be a JSON object")
        base = filename.parent
        if "file_root" in values:
            values["file_root"] = _resolve_path(values["file_root"], base, write_root=True)
        tokenization = values.get("tokenization")
        visual = values.get('visual')
        if isinstance(visual,dict) and visual.get('model_path') is not None:
            visual['model_path'] = _resolve_path(visual['model_path'],base)
        if isinstance(tokenization, dict) and tokenization.get("user_dictionary") is not None:
            tokenization["user_dictionary"] = _resolve_path(tokenization["user_dictionary"], base, regular_file=True)
        models = values.get("models")
        if isinstance(models, dict):
            for model in models.values():
                if isinstance(model, dict) and model.get("model_path") is not None:
                    model["model_path"] = _resolve_path(model["model_path"], base)
        sources = values.get("sources")
        if isinstance(sources, list):
            for source in sources:
                if isinstance(source, dict) and source.get("type") == "filesystem" and "root" in source:
                    source["root"] = _resolve_path(source["root"], base)
        return Settings.model_validate(values)
    except ConfigError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, ValidationError):
        raise ConfigError("invalid knowledge configuration; verify fields, types, scopes and paths") from None
