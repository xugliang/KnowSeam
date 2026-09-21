"""CLI orchestration contracts with controlled stores/models/connectors, no live I/O."""

import argparse
import asyncio
from functools import wraps
import json
from types import SimpleNamespace

from pydantic import SecretStr
import pytest

from knowseam import cli
from knowseam.config import FeishuSourceConfig, load_config


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


@pytest.fixture
def settings(tmp_path):
    value = {
        "environment": "test", "dsn": "postgresql://user:never-print-this-password@localhost/test",
        "file_root": "data", "company_id": "company",
        "tokenization": {"terms": ["示例产品专名"], "user_dictionary": "words.txt"},
        "collections": [{"id": "public", "title": "公共资料"}],
        "clients": [{"token": "never-print-this-token-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                     "product_id": "product-a", "company_id": "company", "allowed_collections": ["public"]}],
        "models": {kind: {"backend": "http", "model_id": kind, "base_url": "http://127.0.0.1:9000",
                           "model_revision": "immutable-v1", "dimensions": 32}
                   for kind in ("embedding", "reranker")},
        "sources": [{"type": "filesystem", "id": "local", "collection_id": "public", "root": "input",
                     "public_files": ["readme.txt"]}],
    }
    path = tmp_path / "config.json"
    (tmp_path / "words.txt").write_text("示例产品词典 10000000 nz\n", encoding="utf-8")
    path.write_text(json.dumps(value), encoding="utf-8")
    return load_config(path)


class Store:
    instances = []

    def __init__(self, dsn, **kwargs):
        self.calls = []
        self.available = True
        self.source_status = "ready"
        self.instances.append(self)

    def initialize(self):
        self.calls.append("initialize")

    def configure(self, **kwargs):
        self.calls.append(("configure", kwargs))

    def ready(self):
        self.calls.append("ready")
        return self.available

    def status(self):
        self.calls.append("status")
        return [{"id": "local", "status": self.source_status, "counts": {"failed": 0}}]

    def close(self):
        self.calls.append("close")


class Model:
    dimensions = 32

    def __init__(self, name, events):
        self.model_id, self.events = name, events

    async def embed(self, texts, *, query=False):
        self.events.append(("embed", tuple(texts), query))
        return [[1.0] * self.dimensions for _ in texts]

    async def score(self, query, documents):
        self.events.append(("rerank", query, tuple(documents)))
        return [1.0 for _ in documents]

    async def aclose(self):
        self.events.append(("close-model", self.model_id))


@pytest.fixture
def runtime(monkeypatch):
    Store.instances = []
    events = []
    monkeypatch.setattr(cli, "PostgresStore", Store)
    monkeypatch.setattr(cli, "build_model", lambda model, **kwargs: Model(model.model_id, events))
    return events


@pytest.mark.parametrize("command", ["init-db", "status"])
@async_test
async def test_database_commands_never_construct_models(settings, monkeypatch, runtime, capsys, command):
    monkeypatch.setattr(cli, "build_model", lambda *args, **kwargs: pytest.fail("database command loaded a model"))
    code = await cli.execute(argparse.Namespace(command=command), settings)
    assert code == 0
    calls = Store.instances[-1].calls
    assert calls[-1] == "close"
    if command == "init-db":
        assert calls[0] == "initialize"
        assert calls[1][0] == "configure"
        configured = calls[1][1]
        assert configured["company_id"] == "company"
        assert configured["sources"][0]["config_hash"] == cli.source_config_hash(settings.sources[0])
    else:
        assert calls == ["ready", "status", "close"]
    assert "never-print" not in capsys.readouterr().out


@async_test
async def test_model_check_runs_real_service_warmup_with_non_sensitive_fixtures(settings, runtime, capsys):
    code = await cli.execute(argparse.Namespace(command="check-models"), settings)
    assert code == 0
    assert [event[0] for event in runtime] == ["embed", "rerank", "close-model", "close-model"]
    assert Store.instances[-1].calls == ["close"]  # Model check does not require/mutate a database.
    output = json.loads(capsys.readouterr().out)
    assert output["models_ready"] is True
    assert output["embedding_dimensions"] == 32
    assert "never-print" not in json.dumps(runtime)


@async_test
async def test_serve_warms_up_before_starting_and_closes_resources(settings, runtime, monkeypatch):
    import uvicorn
    import knowseam.api as api_module
    captured = {}
    original_create_app = api_module.create_app

    def create_app(service, clients):
        captured["tokenizer"] = service.tokenizer
        return original_create_app(service, clients)

    def server_config(app, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(app=app)

    class Server:
        def __init__(self, config):
            self.config = config

        async def serve(self):
            runtime.append(("serve",))

    monkeypatch.setattr(uvicorn, "Config", server_config)
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(api_module, "create_app", create_app)
    code = await cli.execute(argparse.Namespace(command="serve", host="127.0.0.1", port=18790), settings)
    assert code == 0
    assert [event[0] for event in runtime] == ["embed", "rerank", "serve", "close-model", "close-model"]
    assert captured["host"] == "127.0.0.1"
    assert captured["access_log"] is False
    assert captured["tokenizer"].terms == settings.tokenization.terms
    assert captured["tokenizer"].user_dictionary == settings.tokenization.user_dictionary


@async_test
async def test_sync_threads_config_hash_force_and_closes_connectors(settings, runtime, monkeypatch, capsys):
    import knowseam.sync as sync_module
    calls = []
    tokenizers = []

    class Connector:
        async def close(self):
            calls.append("connector-close")

    class Worker:
        def __init__(self, store, *args, **kwargs):
            self.store = store
            tokenizers.append(args[2])

        async def run(self, source_id, connector, **kwargs):
            calls.append((source_id, kwargs))
            return {"seen": 1, "published": 1, "failed": 0}

    monkeypatch.setattr(sync_module, "SyncWorker", Worker)
    monkeypatch.setattr(cli, "build_connector", lambda source: Connector())
    code = await cli.execute(argparse.Namespace(command="sync", force=True), settings)
    assert code == 0
    assert calls[0] == ("local", {"force": True, "config_hash": cli.source_config_hash(settings.sources[0])})
    assert calls[-1] == "connector-close"
    assert tokenizers[0].terms == settings.tokenization.terms
    assert tokenizers[0].user_dictionary == settings.tokenization.user_dictionary
    assert [event[0] for event in runtime] == ["embed", "close-model"]  # Worker needs no reranker.
    assert json.loads(capsys.readouterr().out)["sources"][0]["complete"] is True


@async_test
async def test_worker_initial_daily_force_and_failure_does_not_advance(settings, caplog):
    class Worker:
        def __init__(self):
            self.store = Store("dummy")
            self.calls = []
            self.fail_once = False

        async def run(self, source, connector, **kwargs):
            self.calls.append(kwargs["force"])
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("never-print-source-body")
            return {"failed": 0}

    async def drive(worker, timestamps, cycles):
        values = iter(timestamps)
        count = 0

        async def stop_after(delay):
            nonlocal count
            count += 1
            assert delay > 0
            if count == cycles:
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await cli.run_worker(worker, [(settings.sources[0], object())], interval_seconds=300,
                                 sleep=stop_after, monotonic=lambda: next(values))

    worker = Worker()
    await drive(worker, [0, 0, 300, 300, 86400, 86400], 3)
    assert worker.calls == [True, False, True]
    worker = Worker()
    worker.fail_once = True
    await drive(worker, [0, 0, 300, 300], 2)
    assert worker.calls == [True, True]
    assert "RuntimeError" in caplog.text
    assert "never-print-source-body" not in caplog.text


@async_test
async def test_partial_scan_is_not_reported_complete(settings):
    store = Store("dummy")
    store.source_status = "partial"

    async def run(*args, **kwargs):
        return {"failed": 0}

    worker = SimpleNamespace(store=store, run=run)
    output = await cli.sync_round(worker, [(settings.sources[0], object())])
    assert output[0]["complete"] is False


def test_source_hash_ignores_secret_but_changes_with_source_semantics():
    source = FeishuSourceConfig(type="feishu", id="wiki", collection_id="public", app_id="app",
                                app_secret="private-secret-one", root_token="root-one")
    rotated = source.model_copy(update={"app_secret": SecretStr("private-secret-two")})
    relocated = source.model_copy(update={"root_token": "root-two"})
    assert cli.source_config_hash(source) == cli.source_config_hash(rotated)
    assert cli.source_config_hash(source) != cli.source_config_hash(relocated)


@pytest.mark.parametrize("embedding", [True, False])
def test_model_factory_forwards_revision_only_for_embedding(settings, monkeypatch, embedding):
    import knowseam.models as model_module
    captured = {}

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(model_module, "HTTPEmbedder", factory)
    monkeypatch.setattr(model_module, "HTTPReranker", factory)
    cli.build_model(settings.models.embedding, embedding=embedding)
    assert captured["model_id"] == "embedding"
    assert captured["allow_online"] is False
    if embedding:
        assert captured["model_revision"] == "immutable-v1"
        assert captured["dimensions"] == 32
    else:
        assert "model_revision" not in captured


def test_parser_defaults_bind_loopback_and_validates_commands():
    args = cli.parser().parse_args(["--config", "settings.json", "serve"])
    assert (args.host, args.port) == ("127.0.0.1", 18790)
    assert cli.parser().parse_args(["--config", "settings.json", "sync", "--force"]).force


def test_command_errors_do_not_expose_configuration_exception_or_stack(monkeypatch, capsys, caplog):
    def fail(path):
        raise RuntimeError("never-print-password-and-sql")

    monkeypatch.setattr(cli, "load_config", fail)
    assert cli.main(["--config", "settings.json", "status"]) == 1
    output = capsys.readouterr()
    payload = json.loads(output.err)
    assert payload["error"] == "RuntimeError"
    assert payload["error_id"]
    assert "never-print" not in output.out + output.err + caplog.text
    assert "Traceback" not in output.err


def test_bad_port_rejected_without_loading_configuration(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda _: pytest.fail("invalid port must fail before configuration"))
    assert cli.main(["--config", "settings.json", "serve", "--port", "65536"]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "ValueError"
