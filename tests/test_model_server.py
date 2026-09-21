"""HTTP/lifecycle CONTRACT tests; doubles do not assert pretrained-model quality."""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from knowseam import model_server
from knowseam.model_server import ModelServerConfig, build_app, create_app, parse_args, read_token
from knowseam.models import HTTPEmbedder, HTTPReranker, ModelError, query_instruction

TOKEN = "model-contract-token-only-0123456789-ABCDEF"


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


class ContractEmbedder:
    model_id = "contract-embedding"
    dimensions = 32

    def __init__(self):
        self.calls = []
        self.failure = None
        self.output = None
        self.wait = None
        self.started = asyncio.Event()

    async def embed(self, texts, *, query=False):
        self.calls.append((texts, query))
        self.started.set()
        if self.wait is not None:
            await self.wait.wait()
        if self.failure is not None:
            raise self.failure
        return self.output if self.output is not None else [[1.0] + [0.0] * 31 for _ in texts]


class ContractReranker:
    model_id = "contract-reranking"

    def __init__(self):
        self.calls = []
        self.failure = None
        self.output = None

    async def score(self, query, documents):
        self.calls.append((query, documents))
        if self.failure is not None:
            raise self.failure
        return self.output if self.output is not None else [float(index + 1) / len(documents)
                                                          for index in range(len(documents))]


@asynccontextmanager
async def running(embedder=None, reranker=None, *, token=None, wait_ready=True):
    embedder, reranker = embedder or ContractEmbedder(), reranker or ContractReranker()
    app = create_app(embedder, reranker, token=token)
    async with app.router.lifespan_context(app):
        if wait_ready:
            await app.state.warmup_task
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://model") as client:
            yield app, client, embedder, reranker


def embedding_body(**kwargs):
    return {"model": "contract-embedding", "input": ["公司产品资料"], "dimensions": 32, **kwargs}


def rerank_body(**kwargs):
    return {"model": "contract-reranking", "query": "产品功能", "documents": ["甲", "乙"], **kwargs}


@async_test
async def test_real_nonempty_warmup_required_before_ready():
    embedder = ContractEmbedder()
    embedder.wait = asyncio.Event()
    async with running(embedder, wait_ready=False) as (app, client, _, reranker):
        await embedder.started.wait()
        assert (await client.get("/health/ready")).status_code == 503
        response = await client.post("/v1/embeddings", json=embedding_body())
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "warming_up"
        assert len(embedder.calls) == 1 and embedder.calls[0][0] and not reranker.calls
        embedder.wait.set()
        await app.state.warmup_task
        assert len(reranker.calls) == 1 and reranker.calls[0][1]
        assert (await client.get("/health/ready")).json() == {"status": "ready"}


@async_test
async def test_not_started_is_never_ready():
    app = create_app(ContractEmbedder(), ContractReranker())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://model") as client:
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "not_started"


@pytest.mark.parametrize("failure", [ModelError("private weight path /secret and token"), RuntimeError("secret")])
@async_test
async def test_failed_warmup_stays_unready_without_secret(failure, caplog):
    embedder = ContractEmbedder()
    embedder.failure = failure
    async with running(embedder) as (_, client, _, reranker):
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "model_error"
        assert not reranker.calls
    assert "secret" not in response.text + caplog.text


@async_test
async def test_http_adapters_roundtrip_preserves_single_query_instruction():
    async with running() as (app, _, embedder, reranker):
        transport = httpx.ASGITransport(app=app)
        http_embedder = HTTPEmbedder("http://localhost", model_id=embedder.model_id, dimensions=32,
                                    transport=transport)
        http_reranker = HTTPReranker("http://localhost", model_id=reranker.model_id, transport=transport)
        vectors = await http_embedder.embed(["如何开票"], query=True)
        scores = await http_reranker.score("如何开票", ["积分页面", "订单开票"])
        assert vectors == [[1.0] + [0.0] * 31]
        assert embedder.calls[-1] == ([query_instruction("如何开票")], False)
        assert scores == [.5, 1.0]
        assert reranker.calls[-1] == ("如何开票", ["积分页面", "订单开票"])


@async_test
async def test_embedding_string_input_and_reranker_top_n():
    async with running() as (_, client, embedder, _):
        response = await client.post("/v1/embeddings", json=embedding_body(input="文档"))
        assert response.status_code == 200
        assert embedder.calls[-1] == (["文档"], False)
        assert response.json()["data"][0]["index"] == 0
        ranked = await client.post("/v1/rerank", json=rerank_body(top_n=1))
        assert ranked.json()["results"] == [{"index": 1, "relevance_score": 1.0}]
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("change", [
    {"input": []}, {"input": [""]}, {"input": [True]}, {"input": ["x"] * 129},
    {"input": ["x" * 65537]}, {"input": {"text": "ignored"}},
    {"dimensions": 64}, {"dimensions": True}, {"dimensions": "32"},
    {"model": "another"}, {"encoding_format": "base64"}, {"unexpected": "secret"},
])
@async_test
async def test_invalid_embeddings_rejected_before_inference(change):
    async with running() as (_, client, embedder, _):
        calls = len(embedder.calls)
        response = await client.post("/v1/embeddings", json=embedding_body(**change))
        assert response.status_code == 400
        assert len(embedder.calls) == calls
        assert "secret" not in response.text


@pytest.mark.parametrize("change", [
    {"query": ""}, {"query": None}, {"documents": []}, {"documents": ["x"] * 129},
    {"documents": [{"text": "text"}]}, {"top_n": 0}, {"top_n": 3}, {"top_n": True},
    {"model": "another"}, {"return_documents": True},
])
@async_test
async def test_invalid_rerank_rejected_before_inference(change):
    async with running() as (_, client, _, reranker):
        calls = len(reranker.calls)
        response = await client.post("/v1/rerank", json=rerank_body(**change))
        assert response.status_code == 400
        assert len(reranker.calls) == calls


@async_test
async def test_token_required_on_all_routes_and_duplicates_rejected():
    async with running(token=TOKEN) as (_, client, _, _):
        for headers in [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic " + TOKEN},
                        [("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)]]:
            response = await client.get("/health/ready", headers=headers)
            assert response.status_code == 401
            assert TOKEN not in response.text
        response = await client.get("/health/ready", headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 200
        response = await client.post("/v1/embeddings", json=embedding_body())
        assert response.status_code == 401


@async_test
async def test_bad_json_encoding_media_type_and_body_bound(monkeypatch):
    async with running() as (_, client, embedder, _):
        calls = len(embedder.calls)
        for raw in ['{"model":"a","model":"b"}', '{"input":NaN}', '["not object"]', '{broken']:
            response = await client.post("/v1/embeddings", content=raw, headers={"Content-Type": "application/json"})
            assert response.status_code == 400
        response = await client.post("/v1/embeddings", content="text")
        assert response.status_code == 415
        response = await client.post("/v1/embeddings", json=embedding_body(), headers={"Content-Encoding": "gzip"})
        assert response.status_code == 415
        monkeypatch.setattr(model_server, "MAX_BODY_BYTES", 32)
        response = await client.post("/v1/embeddings", json=embedding_body())
        assert response.status_code == 413

        async def chunked():
            yield b" " * 20
            yield b" " * 20

        response = await client.post("/v1/embeddings", content=chunked(), headers={"Content-Type": "application/json"})
        assert response.status_code == 413
        assert len(embedder.calls) == calls


@async_test
async def test_query_parameters_rejected():
    async with running() as (_, client, _, _):
        response = await client.get("/health/ready?token=secret")
        assert response.status_code == 400
        assert "secret" not in response.text


@async_test
async def test_concurrent_other_model_rejected_without_queue():
    async with running() as (_, client, embedder, reranker):
        embedder.wait = asyncio.Event()
        embedder.started.clear()
        request = asyncio.create_task(client.post("/v1/embeddings", json=embedding_body()))
        await embedder.started.wait()
        count = len(reranker.calls)
        response = await client.post("/v1/rerank", json=rerank_body())
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "model_busy"
        assert len(reranker.calls) == count
        embedder.wait.set()
        assert (await request).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200


@pytest.mark.parametrize("bad_output", [[[0.0] * 32], [[float("nan")] * 32], [[1.0]], []])
@async_test
async def test_invalid_model_output_503_and_revokes_ready(bad_output):
    async with running() as (_, client, embedder, reranker):
        embedder.output = bad_output
        response = await client.post("/v1/embeddings", json=embedding_body())
        assert response.status_code == 503
        assert (await client.get("/health/ready")).status_code == 503
        calls = len(reranker.calls)
        assert (await client.post("/v1/rerank", json=rerank_body())).status_code == 503
        assert len(reranker.calls) == calls


@async_test
async def test_model_failure_never_discloses_exception(caplog):
    async with running() as (_, client, embedder, _):
        embedder.failure = ModelError("secret company text and API token")
        response = await client.post("/v1/embeddings", json=embedding_body())
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "model_error"
    assert "secret" not in response.text + caplog.text


@async_test
async def test_reranker_warmup_must_validate_actual_scores():
    reranker = ContractReranker()
    reranker.output = [float("nan")]
    async with running(reranker=reranker) as (_, client, _, _):
        assert (await client.get("/health/ready")).status_code == 503
        assert len(reranker.calls) == 1


@async_test
async def test_cancelled_inference_revokes_ready_to_prevent_overlapping_workers():
    async with running() as (_, client, embedder, reranker):
        embedder.wait = asyncio.Event()
        embedder.started.clear()
        request = asyncio.create_task(client.post("/v1/embeddings", json=embedding_body()))
        await embedder.started.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert (await client.get("/health/ready")).status_code == 503
        calls = len(reranker.calls)
        assert (await client.post("/v1/rerank", json=rerank_body())).status_code == 503
        assert len(reranker.calls) == calls


def test_safe_cli_defaults_and_no_token_argument():
    config = parse_args(["--model-root", "models"])
    assert (config.host, config.port, config.device, config.dtype, config.threads, config.batch_size) == (
        "127.0.0.1", 18792, "cpu", "float32", 2, 2,
    )
    assert config.token_file is None
    with pytest.raises(SystemExit):
        parse_args(["--model-root", "models", "--token", "should-not-be-on-command-line"])


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.22", "models.local"])
def test_nonloopback_bind_requires_token_file(host):
    with pytest.raises(ModelError, match="token-file"):
        ModelServerConfig(Path("models"), host=host)
    assert ModelServerConfig(Path("models"), host=host, token_file=Path("token-file")).host == host


def test_loopback_variants_do_not_need_token_file():
    for host in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
        assert ModelServerConfig(Path("models"), host=host).token_file is None


@pytest.mark.parametrize("override", [{"threads": 0}, {"threads": 33}, {"batch_size": True},
                                      {"batch_size": 129}, {"port": 0}, {"max_length": 1}])
def test_resource_bounds(override):
    with pytest.raises(ModelError):
        ModelServerConfig(Path("models"), **override)


def test_token_file_validation_and_snapshot(tmp_path):
    path = tmp_path / "contract-token.txt"
    path.write_bytes((TOKEN + "\r\n").encode())
    assert read_token(path) == TOKEN
    for content in (b"short", b"x" * 515, b"x" * 33 + b"\xff", b"x" * 40 + b"\n\n"):
        path.write_bytes(content)
        with pytest.raises(ModelError):
            read_token(path)
    with pytest.raises(ModelError, match="cannot read"):
        read_token(tmp_path / "absent")


def test_build_uses_expected_subdirs_and_explicit_cpu_options(tmp_path, monkeypatch):
    calls = []

    def embedder(path, **options):
        calls.append((path, options))
        return ContractEmbedder()

    def reranker(path, **options):
        calls.append((path, options))
        return ContractReranker()

    monkeypatch.setattr(model_server, "Qwen3Embedder", embedder)
    monkeypatch.setattr(model_server, "Qwen3Reranker", reranker)
    build_app(ModelServerConfig(tmp_path))
    assert [path.name for path, _ in calls] == ["Qwen3-Embedding-0.6B", "Qwen3-Reranker-0.6B"]
    assert all(options["device"] == "cpu" and options["dtype"] == "float32" and options["batch_size"] == 2
               for _, options in calls)


def test_threads_bounded_without_gpu_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(model_server.importlib, "import_module", lambda name: SimpleNamespace(
        set_num_threads=lambda value: calls.append(("intra", value)),
        set_num_interop_threads=lambda value: calls.append(("inter", value))))
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "TOKENIZERS_PARALLELISM"):
        monkeypatch.setenv(name, "contract-previous-value")
    model_server.configure_threads(2)
    assert calls == [("intra", 2), ("inter", 1)]
    assert model_server.os.environ["OMP_NUM_THREADS"] == "2"
    assert model_server.os.environ["TOKENIZERS_PARALLELISM"] == "false"
