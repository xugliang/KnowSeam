"""Adapter CONTRACT tests with deterministic HTTP/model doubles, not model quality tests."""

import asyncio
import json
import math
import threading
from types import SimpleNamespace

import httpx
import pytest

from knowseam import models
from knowseam.models import (
    HTTPEmbedder, HTTPReranker, ModelError, Qwen3Embedder, Qwen3Reranker,
    query_instruction, rerank_pair, validate_embeddings, validate_scores, validate_texts,
)


def vector(index=0):
    values = [0.0] * 32
    values[index] = 2.0
    return values


def local_path(tmp_path):
    # This marker is NOT a downloaded/working model. Loaders are explicit doubles.
    path = tmp_path / "contract-only-not-a-model"
    path.mkdir()
    (path / "config.json").write_text("{}", encoding="utf-8")
    return path


def test_http_embedding_contract_orders_and_normalizes_query():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer test-only-key"
        return httpx.Response(200, json={"model": "contract-embed", "data": [
            {"index": 1, "embedding": vector(1)}, {"index": 0, "embedding": vector(0)},
        ]})

    client = HTTPEmbedder("http://127.0.0.1:8080", model_id="contract-embed", dimensions=32,
                          api_key="test-only-key", transport=httpx.MockTransport(handler))
    output = asyncio.run(client.embed(["示例产品怎么用", "Kino如何创建项目"], query=True))
    assert calls[0]["input"] == [query_instruction("示例产品怎么用"), query_instruction("Kino如何创建项目")]
    assert calls[0]["dimensions"] == 32
    assert output[0][0] == output[1][1] == 1


def test_http_document_embedding_does_not_add_query_instruction():
    def handler(request):
        assert json.loads(request.content)["input"] == ["公司产品资料"]
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector()}]})

    client = HTTPEmbedder("http://localhost:8000", model_id="contract", dimensions=32,
                          transport=httpx.MockTransport(handler))
    asyncio.run(client.embed(["公司产品资料"]))


def test_http_rerank_contract_restores_input_order():
    def handler(request):
        assert request.url.path == "/v1/rerank"
        assert json.loads(request.content) == {
            "model": "contract-rerank", "query": "如何开票", "documents": ["积分流水", "订单开票"], "top_n": 2,
        }
        return httpx.Response(200, json={"results": [
            {"index": 1, "relevance_score": .9}, {"index": 0, "relevance_score": .1},
        ]})

    client = HTTPReranker("http://[::1]:8000", model_id="contract-rerank", transport=httpx.MockTransport(handler))
    assert asyncio.run(client.score("如何开票", ["积分流水", "订单开票"])) == [.1, .9]


@pytest.mark.parametrize("url", ["http://192.0.2.22:8000", "http://models:8000", "https://api.example.com"])
def test_nonloopback_requires_explicit_permission(url):
    with pytest.raises(ModelError, match="allow_online"):
        HTTPReranker(url, model_id="contract")
    # Constructor only: no network operation is performed by this test.
    assert HTTPReranker(url, model_id="contract", allow_online=True).base_url == url


@pytest.mark.parametrize("url", [
    "file:///tmp/weights", "http://u:p@localhost", "http://localhost:bad",
    "http://localhost?q=secret", "http://localhost#fragment", "http:///missing",
])
def test_invalid_model_url_rejected(url):
    with pytest.raises(ModelError):
        HTTPReranker(url, model_id="contract")


@pytest.mark.parametrize("data", [
    [], [{"index": 0, "embedding": [1]}], [{"index": 1, "embedding": vector()}],
    [{"index": True, "embedding": vector()}], [{"index": 0, "embedding": [0] * 32}],
    [{"index": 0, "embedding": [float("nan")] * 32}],
])
def test_bad_embedding_output_fails_without_fallback(data):
    def handler(request):
        # NaN is intentionally raw invalid model output, not generated knowledge.
        return httpx.Response(200, content=json.dumps({"data": data}).encode())

    client = HTTPEmbedder("http://localhost", model_id="contract", dimensions=32,
                          transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError):
        asyncio.run(client.embed(["公司资料"]))


def test_duplicate_indices_and_wrong_model_are_rejected():
    for payload in [
        {"model": "wrong-model", "data": []},
        {"data": [{"index": 0, "embedding": vector()}, {"index": 0, "embedding": vector()}]},
    ]:
        client = HTTPEmbedder("http://localhost", model_id="contract", dimensions=32,
                              transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
        with pytest.raises(ModelError):
            asyncio.run(client.embed(["甲", "乙"]))


@pytest.mark.parametrize("results", [
    [], [{"index": 0, "relevance_score": "0.5"}], [{"index": -1, "relevance_score": .5}],
    [{"index": 0, "relevance_score": True}], [{"index": 0, "relevance_score": float("inf")}],
])
def test_bad_rerank_output_fails(results):
    client = HTTPReranker("http://localhost", model_id="contract", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=json.dumps({"results": results}).encode())))
    with pytest.raises(ModelError):
        asyncio.run(client.score("提问", ["资料"]))


def test_http_failure_is_not_retried_and_does_not_disclose_body():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="private company question and secret-token")

    client = HTTPReranker("http://localhost", model_id="contract", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError, match="503") as error:
        asyncio.run(client.score("公司秘密", ["资料"]))
    assert "secret" not in str(error.value)
    assert len(calls) == 1


def test_http_redirect_never_followed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://outside.example/v1/rerank"})

    client = HTTPReranker("http://localhost", model_id="contract", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError, match="307"):
        asyncio.run(client.score("提问", ["资料"]))
    assert len(calls) == 1


def test_http_total_timeout_and_response_size_bound(monkeypatch):
    async def slow(request):
        await asyncio.sleep(.1)
        return httpx.Response(200, json={"results": []})

    client = HTTPReranker("http://localhost", model_id="contract", timeout=.005,
                         transport=httpx.MockTransport(slow))
    with pytest.raises(ModelError, match="timed out"):
        asyncio.run(client.score("提问", ["资料"]))
    monkeypatch.setattr(models, "MAX_RESPONSE_BYTES", 32)
    client = HTTPReranker("http://localhost", model_id="contract", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 64)))
    with pytest.raises(ModelError, match="size limit"):
        asyncio.run(client.score("提问", ["资料"]))


def test_text_limits_and_numeric_validation():
    for value in ["not-list", [""], [" "], [1], ["x"] * 129, ["x" * 65537]]:
        with pytest.raises(ModelError):
            validate_texts(value)
    for values in [[float("nan")], [float("inf")], [True], ["1"], []]:
        with pytest.raises(ModelError):
            validate_scores(values, 1)
    assert math.isclose(sum(value * value for value in validate_embeddings([vector()], 1, 32)[0]), 1)


def test_local_path_missing_fails_without_download(tmp_path):
    with pytest.raises(ModelError, match="downloads are disabled"):
        Qwen3Embedder(tmp_path / "not-present")


def test_local_embedding_identity_isolates_semantic_configuration(tmp_path):
    path = local_path(tmp_path)
    baseline = Qwen3Embedder(path, dimensions=32, model_revision="weights-r1")
    assert baseline.cache_identity == Qwen3Embedder(
        path, dimensions=32, model_revision="weights-r1", timeout=5, batch_size=1,
    ).cache_identity
    for changed in [
        {"instruction": "Retrieve company product details"}, {"model_revision": "weights-r2"},
        {"dimensions": 64}, {"max_length": 2048}, {"dtype": "bfloat16"}, {"model_id": "other"},
    ]:
        options = {"dimensions": 32, "model_revision": "weights-r1", **changed}
        assert baseline.cache_identity != Qwen3Embedder(path, **options).cache_identity
    assert baseline.model_id == "Qwen/Qwen3-Embedding-0.6B"


def test_http_embedding_identity_separates_configuration_not_credentials():
    def make(url="http://localhost:8000", **options):
        return HTTPEmbedder(url, **{"model_id": "contract", "dimensions": 32,
                                   "model_revision": "weights-r1", **options})

    baseline = make()
    assert baseline.cache_identity == make(api_key="another-test-key", timeout=5).cache_identity
    assert baseline.cache_identity == make("http://localhost:8000/").cache_identity
    assert baseline.cache_identity != make("http://localhost:8001").cache_identity
    for changed in [{"model_revision": "weights-r2"}, {"model_id": "other"}, {"dimensions": 64},
                    {"instruction": "Retrieve company product details"}]:
        assert baseline.cache_identity != make(**changed).cache_identity
    assert baseline.model_id == "contract"
    assert "another-test-key" not in make(api_key="another-test-key").cache_identity


@pytest.mark.parametrize("revision", [None, True, "x" * 257, "value\nsecret"])
def test_embedding_identity_rejects_invalid_revision(revision):
    with pytest.raises(ModelError, match="model_revision"):
        HTTPEmbedder("http://localhost", model_id="contract", model_revision=revision)


def test_local_load_is_lazy_local_only_and_architecture_checked(tmp_path, monkeypatch):
    path = local_path(tmp_path)
    calls = []

    class ContractModel:
        config = SimpleNamespace(model_type="qwen3", max_position_embeddings=32768)

        def to(self, device):
            calls.append(("device", device))
            return self

        def eval(self):
            return self

    class Factory:
        @staticmethod
        def from_pretrained(path, **kwargs):
            calls.append(kwargs)
            return ContractModel()

    fake_transformers = SimpleNamespace(AutoTokenizer=Factory, AutoModel=Factory, AutoModelForCausalLM=Factory)
    original_import = models.importlib.import_module

    def imports(name):
        if name == "torch":
            return SimpleNamespace(float32="contract-float32")
        if name == "transformers":
            return fake_transformers
        return original_import(name)

    monkeypatch.setattr(models.importlib, "import_module", imports)
    embedder = Qwen3Embedder(path)
    assert calls == []
    embedder._load(causal=False)
    assert all(call["local_files_only"] and call["trust_remote_code"] is False
               for call in calls if isinstance(call, dict))
    assert embedder._model is not None


def test_local_missing_optional_dependency_is_actionable(tmp_path, monkeypatch):
    embedder = Qwen3Embedder(local_path(tmp_path))

    def missing(name):
        raise ImportError("contract missing torch")

    monkeypatch.setattr(models.importlib, "import_module", missing)
    with pytest.raises(ModelError, match="optional"):
        asyncio.run(embedder.embed(["公司资料"]))


def test_local_query_template_and_empty_batch_contract(tmp_path, monkeypatch):
    embedder = Qwen3Embedder(local_path(tmp_path), dimensions=32)
    seen = []

    def contract_inference(texts):
        seen.extend(texts)
        return [vector() for _ in texts]

    monkeypatch.setattr(embedder, "_embed_sync", contract_inference)
    assert asyncio.run(embedder.embed([])) == []
    asyncio.run(embedder.embed(["问题"], query=True))
    assert seen == [query_instruction("问题")]
    assert rerank_pair("问题", "资料") == (
        f"<Instruct>: {models.QUERY_INSTRUCTION}\n<Query>: 问题\n<Document>: 资料"
    )
    assert 'only be "yes" or "no"' in models.RERANK_PREFIX
    assert "<think>\n\n</think>" in models.RERANK_SUFFIX


def test_local_timeout_keeps_busy_until_worker_finishes(tmp_path, monkeypatch):
    embedder = Qwen3Embedder(local_path(tmp_path), timeout=.01)
    release = threading.Event()

    def contract_worker(texts):
        release.wait(1)
        return [vector()]

    monkeypatch.setattr(embedder, "_embed_sync", contract_worker)

    async def scenario():
        try:
            with pytest.raises(ModelError, match="timed out"):
                await embedder.embed(["资料"])
            with pytest.raises(ModelError, match="busy"):
                await embedder.embed(["资料"])
        finally:
            release.set()
            await asyncio.sleep(.01)

    asyncio.run(scenario())


def test_real_tensor_pool_and_reranker_math_contract(tmp_path, monkeypatch):
    """Requires optional torch, but never downloads or runs pretrained weights."""
    torch = pytest.importorskip("torch", reason="real tensor contract requires optional torch; no model quality test")
    states = torch.tensor([[[1., 0.], [2., 0.], [3., 0.]], [[4., 0.], [5., 0.], [6., 0.]]])
    assert models.last_token_pool(states, torch.tensor([[0, 1, 1], [1, 1, 1]])).tolist() == [[3., 0.], [6., 0.]]
    assert models.last_token_pool(states, torch.tensor([[1, 1, 0], [1, 0, 0]])).tolist() == [[2., 0.], [4., 0.]]

    class Batch(dict):
        def to(self, device):
            return self

    class ContractTokenizer:
        def encode(self, text, **kwargs):
            return {"yes": [1], "no": [0]}.get(text, [2])

        def __call__(self, pairs, **kwargs):
            assert all("<Query>: 如何开票" in pair and "<Document>:" in pair for pair in pairs)
            assert kwargs["add_special_tokens"] is False
            return {"input_ids": [[3] for _ in pairs]}

        def pad(self, tokens, **kwargs):
            assert tokens["input_ids"] == [[2, 3, 2]]
            return Batch(input_ids=torch.tensor(tokens["input_ids"]))

    reranker = Qwen3Reranker(local_path(tmp_path))
    monkeypatch.setattr(reranker, "_load", lambda **kwargs: None)
    reranker._torch = torch
    reranker._tokenizer = ContractTokenizer()
    reranker._model = lambda **inputs: SimpleNamespace(logits=torch.tensor([[[0., 2., 0., 0.]]]))
    score = asyncio.run(reranker.score("如何开票", ["订单开票入口"]))[0]
    assert score == pytest.approx(1 / (1 + math.exp(-2)), rel=1e-6)
