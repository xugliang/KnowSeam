"""JSON configuration boundary tests; no network, credentials or model loading."""

import json
import importlib.util
from pathlib import Path

import pytest

from knowseam.config import ConfigError, MAX_CONFIG_BYTES, ServiceClient, load_config

TOKEN = "contract-fixture-0123456789-AbCdEfGhIjKlMnOp"


def valid():
    return {
        "environment": "test", "dsn": "postgresql://user:very-private-password@localhost/knowledge",
        "file_root": "data", "company_id": "company-one",
        "collections": [{"id": "public", "title": "公司资料"}],
        "clients": [{"token": TOKEN, "product_id": "product-a", "company_id": "company-one",
                     "allowed_collections": ["public"]}],
        "models": {
            "embedding": {"backend": "qwen", "model_id": "Qwen/Qwen3-Embedding-0.6B", "model_path": "models/embedding"},
            "reranker": {"backend": "qwen", "model_id": "Qwen/Qwen3-Reranker-0.6B", "model_path": "models/reranker"},
        },
        "sources": [{"id": "local-public", "type": "filesystem", "collection_id": "public", "root": "input",
                     "public_files": ["folder/readme.txt"]}],
    }


def write_config(tmp_path, values):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_load_resolves_paths_and_preserves_secret_representations(tmp_path):
    settings = load_config(write_config(tmp_path, valid()))
    assert settings.file_root == tmp_path / "data"
    assert settings.models.embedding.model_path == tmp_path / "models" / "embedding"
    assert settings.sources[0].root == tmp_path / "input"
    assert settings.sources[0].public_files == ("folder/readme.txt",)
    assert settings.clients[0].allowed_collections == ("public",)
    assert settings.sync_interval_seconds == 300
    assert settings.acl_ttl_seconds == 900
    assert settings.models.embedding.dimensions == 1024
    assert settings.tokenization.terms == ()
    assert settings.tokenization.user_dictionary is None
    assert TOKEN not in repr(settings)
    assert "very-private-password" not in repr(settings)
    assert settings.dsn.get_secret_value().startswith("postgresql://")


@pytest.mark.parametrize("key,value", [
    ("extra", True), ("dsn", "sqlite:///tmp/example.db"), ("sync_interval_seconds", True),
    ("sync_interval_seconds", "300"), ("acl_ttl_seconds", 299), ("company_id", ""),
    ("file_root", "../outside"), ("file_root", "~"), ("file_root", ""),
    ("file_root", str(Path.cwd().anchor)),
])
def test_invalid_top_level_types_paths_and_unknown_fields(tmp_path, key, value):
    data = valid()
    data[key] = value
    with pytest.raises(ConfigError) as caught:
        load_config(write_config(tmp_path, data))
    assert TOKEN not in str(caught.value)
    assert "very-private-password" not in str(caught.value)


@pytest.mark.parametrize("change", [
    {"token": "short"}, {"company_id": "other"}, {"allowed_collections": ["private"]},
    {"allowed_collections": []}, {"allowed_collections": ["public", "public"]},
    {"actor_ids": ["actor", "actor"]}, {"actor_ids": "actor"}, {"unknown": "value"},
    {"token": "x" * 32 + "\r\nX-Actor-Id:other"},
])
def test_invalid_client_configuration(tmp_path, change):
    data = valid()
    data["clients"][0].update(change)
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))


def test_production_rejects_placeholder_tokens(tmp_path):
    data = valid()
    data["environment"] = "production"
    for token in ("x" * 40, "please-replace-me-with-a-secure-token", "test-token-0123456789-AbCdEfGhIjKl"):
        data["clients"][0]["token"] = token
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, data))
    data["clients"][0]["token"] = TOKEN
    assert load_config(write_config(tmp_path, data)).environment == "production"


@pytest.mark.parametrize("mutation", ["duplicate-collection", "duplicate-source", "duplicate-token", "missing-collection"])
def test_relationship_validation(tmp_path, mutation):
    data = valid()
    if mutation == "duplicate-collection":
        data["collections"].append(dict(data["collections"][0]))
    elif mutation == "duplicate-source":
        data["sources"].append(dict(data["sources"][0]))
    elif mutation == "duplicate-token":
        data["clients"].append(dict(data["clients"][0]))
    else:
        data["sources"][0]["collection_id"] = "unknown"
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("path", ["../secret", "/etc/shadow", "C:\\secret", "folder/../../secret", "folder\\secret"])
def test_published_paths_are_relative_and_traversal_free(tmp_path, path):
    data = valid()
    data["sources"][0]["public_files"] = [path]
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))


def test_feishu_config_allows_only_known_connector_options_and_redacts_secret(tmp_path):
    data = valid()
    data["sources"] = [{"type": "feishu", "id": "wiki", "collection_id": "public",
                        "app_id": "cli-app", "app_secret": "feishu-private-secret", "root_token": "wiki-token",
                        "wiki_origin": "https://company.feishu.cn"}]
    settings = load_config(write_config(tmp_path, data))
    assert settings.sources[0].app_secret.get_secret_value() == "feishu-private-secret"
    assert "feishu-private-secret" not in repr(settings)
    data["sources"][0]["wiki_origin"] = "https://feishu.cn.evil.invalid"
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))
    data["sources"][0]["wiki_origin"] = "https://company.feishu.cn"
    data["sources"][0]["allow_write"] = True
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))


def test_http_model_requires_explicit_remote_opt_in(tmp_path):
    data = valid()
    data["models"]["embedding"] = {"backend": "http", "model_id": "embedding", "base_url": "http://127.0.0.1:9000"}
    assert load_config(write_config(tmp_path, data)).models.embedding.allow_online is False
    data["models"]["embedding"]["base_url"] = "http://192.0.2.22:9000"
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))
    data["models"]["embedding"]["allow_online"] = True
    assert load_config(write_config(tmp_path, data)).models.embedding.allow_online is True
    data["models"]["embedding"]["base_url"] = "https://user:password@example.com"
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))


@pytest.mark.parametrize("extra", [
    {"dimensions": 0}, {"dimensions": True}, {"timeout": float("inf")}, {"unknown": "x"},
    {"base_url": "http://localhost"}, {"allow_online": True},
])
def test_invalid_model_options_rejected(tmp_path, extra):
    data = valid()
    data["models"]["embedding"].update(extra)
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))


def test_duplicate_json_keys_non_json_and_oversized_file_rejected(tmp_path):
    path = tmp_path / "settings.json"
    for content in ('{"company_id":"a","company_id":"b"}', "company_id: company", "[]", " " * (MAX_CONFIG_BYTES + 1)):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path)


def test_config_missing_file_has_safe_error(tmp_path):
    with pytest.raises(ConfigError, match="invalid knowledge configuration"):
        load_config(tmp_path / "missing.json")


def test_direct_service_client_hides_token_and_freezes_scopes():
    collections = ["public"]
    client = ServiceClient(TOKEN, "product", "company", collections)
    collections.append("private")
    assert client.allowed_collections == ("public",)
    assert TOKEN not in repr(client)


def test_configured_dictionary_is_resolved_relative_to_config_not_cwd(tmp_path, monkeypatch):
    data = valid()
    dictionary = tmp_path / "words.txt"
    dictionary.write_text("示例产品专名 10000000 nz\n", encoding="utf-8")
    data["tokenization"] = {"terms": ["  示例产品专名  ", "ABC"], "user_dictionary": "words.txt"}
    path = write_config(tmp_path, data)
    other = tmp_path / "other-cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    settings = load_config(path)
    assert settings.tokenization.user_dictionary == dictionary
    assert settings.tokenization.terms == ("  示例产品专名  ", "ABC")


@pytest.mark.parametrize("value", [
    {"unknown": 1}, {"terms": "single-word"}, {"terms": [123]}, {"terms": [True]},
    {"terms": [""]}, {"terms": ["\u3000"]}, {"terms": ["x" * 257]},
    {"terms": ["word\nnext"]}, {"terms": ["hidden\u200bword"]},
    {"user_dictionary": ""}, {"user_dictionary": "../outside.txt"},
    {"user_dictionary": "missing.txt"}, {"user_dictionary": "."},
])
def test_tokenization_rejects_invalid_terms_paths_and_unknown_fields(tmp_path, value):
    data = valid()
    data["tokenization"] = value
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))


def test_dictionary_size_is_bounded_during_config_load(tmp_path):
    dictionary = tmp_path / "oversized.txt"
    with dictionary.open("wb") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    data = valid()
    data["tokenization"] = {"user_dictionary": "oversized.txt"}
    with pytest.raises(ConfigError, match="size limit"):
        load_config(write_config(tmp_path, data))


def test_container_snapshot_preserves_terms_but_refuses_unmapped_dictionary(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "prepare_container_config.py"
    spec = importlib.util.spec_from_file_location("knowledge_config_snapshot_contract", script)
    snapshot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snapshot)
    data = valid()
    data["sources"] = []
    data["models"] = {kind: {"backend": "http", "model_id": kind, "base_url": "http://127.0.0.1:18792"}
                      for kind in ("embedding", "reranker")}
    data["tokenization"] = {"terms": ["示例产品专名"], "user_dictionary": "words.txt"}
    source = tmp_path / "config.local.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(snapshot.SnapshotError, match="user_dictionary needs explicit"):
        snapshot.prepare(tmp_path)
    assert not (tmp_path / "config.container.json").exists()
    data["tokenization"]["user_dictionary"] = None
    raw = json.dumps(data)
    source.write_text(raw, encoding="utf-8")
    assert snapshot.prepare(tmp_path)["status"] == "prepared"
    result = json.loads((tmp_path / "config.container.json").read_text(encoding="utf-8"))
    assert result["tokenization"] == data["tokenization"]
    assert result["file_root"] == "/app/data"
    assert source.read_text(encoding="utf-8") == raw
