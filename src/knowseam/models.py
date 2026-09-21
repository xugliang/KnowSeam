"""Real Qwen3 adapters; optional local dependencies, no synthetic fallback.

Local weights are loaded only from an existing directory. HTTP adapters require
explicit permission before sending company text beyond the loopback interface.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import ipaddress
import json
import math
import threading
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from .domain import Embedder, KnowledgeError, Reranker

QUERY_INSTRUCTION = (
    "Given a question about company knowledge, retrieve relevant passages that answer the question"
)
RERANK_SYSTEM = (
    'Judge whether the Document meets the requirements based on the Query and the Instruct provided. '
    'Note that the answer can only be "yes" or "no".'
)
RERANK_PREFIX = f"<|im_start|>system\n{RERANK_SYSTEM}<|im_end|>\n<|im_start|>user\n"
RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
MAX_INPUTS = 128
MAX_TEXT_CHARS = 65536
MAX_TOTAL_CHARS = 1_000_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
EMBEDDING_CACHE_VERSION = "embedding-cache-v1"


class ModelError(KnowledgeError):
    """Model configuration, transport or output failed; never return invented vectors."""


def _revision(value: str) -> str:
    if not isinstance(value, str) or len(value) > 256 or any(ord(character) < 32 for character in value):
        raise ModelError("model_revision must be a string of at most 256 characters without control characters")
    return value.strip()


def _embedding_identity(configuration: dict[str, Any]) -> str:
    serialized = json.dumps(configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return EMBEDDING_CACHE_VERSION + ":" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ModelError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _positive_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelError("timeout must be numeric")
    if not math.isfinite(value) or not 0 < value <= 600:
        raise ModelError("timeout must be in (0, 600] seconds")
    return float(value)


def validate_texts(texts: Sequence[str]) -> list[str]:
    if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
        raise ModelError("texts must be a sequence of strings")
    if len(texts) > MAX_INPUTS:
        raise ModelError("too many model inputs")
    total = 0
    result = []
    for item in texts:
        if not isinstance(item, str) or not item.strip():
            raise ModelError("model inputs must be nonempty strings")
        if len(item) > MAX_TEXT_CHARS:
            raise ModelError("model input exceeds character limit")
        total += len(item)
        if total > MAX_TOTAL_CHARS:
            raise ModelError("model batch exceeds character limit")
        result.append(item)
    return result


def query_instruction(text: str, instruction: str = QUERY_INSTRUCTION) -> str:
    return f"Instruct: {instruction}\nQuery: {text}"


def _instruction(text: str) -> str:
    result = validate_texts([text])[0]
    if len(result) > 2048:
        raise ModelError("model instruction exceeds 2048 characters")
    return result


def rerank_pair(query: str, document: str, instruction: str = QUERY_INSTRUCTION) -> str:
    return f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}"


def validate_scores(values: Any, expected: int) -> list[float]:
    if not isinstance(values, (list, tuple)) or len(values) != expected:
        raise ModelError("reranker returned an unexpected result count")
    result = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ModelError("reranker returned a nonfinite or nonnumeric score")
        result.append(float(value))
    return result


def validate_embeddings(values: Any, expected: int, dimensions: int) -> list[list[float]]:
    if not isinstance(values, (list, tuple)) or len(values) != expected:
        raise ModelError("embedder returned an unexpected result count")
    output = []
    for vector in values:
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
            raise ModelError("embedding dimension mismatch")
        numbers = validate_scores(vector, dimensions)
        # hypot avoids overflow from summing squares of untrusted HTTP values.
        norm = math.hypot(*numbers)
        if norm == 0 or not math.isfinite(norm):
            raise ModelError("embedding must have a finite, nonzero norm")
        output.append([number / norm for number in numbers])
    return output


def last_token_pool(last_hidden_states: Any, attention_mask: Any) -> Any:
    """Qwen's last non-padding token pooling; handles left and right padding."""
    torch = importlib.import_module("torch")
    if bool((attention_mask[:, -1].sum() == attention_mask.shape[0]).item()):
        return last_hidden_states[:, -1]
    sequence_lengths = attention_mask.sum(dim=1) - 1
    batch = torch.arange(last_hidden_states.shape[0], device=last_hidden_states.device)
    return last_hidden_states[batch, sequence_lengths]


class _LocalQwen:
    def __init__(
        self, model_path: str | Path, *, model_id: str, device: str = "cpu",
        dtype: str = "float32", max_length: int = 4096, batch_size: int = 8,
        timeout: float = 120,
    ) -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        if not self.model_path.is_dir() or not (self.model_path / "config.json").is_file():
            raise ModelError("local model directory with config.json is required; downloads are disabled")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ModelError("model_id is required")
        if dtype not in {"float32", "float16", "bfloat16"}:
            raise ModelError("dtype must be float32, float16 or bfloat16")
        if not isinstance(device, str) or not device.strip():
            raise ModelError("device is required")
        self.model_id = model_id
        self.device = device
        self.dtype = dtype
        self.max_length = _integer(max_length, "max_length", 64, 32768)
        self.batch_size = _integer(batch_size, "batch_size", 1, 64)
        self.timeout = _positive_timeout(timeout)
        self._model: Any = None
        self._tokenizer: Any = None
        self._torch: Any = None
        self._busy = threading.Lock()

    def _load(self, *, causal: bool) -> None:
        if self._model is not None:
            return
        try:
            torch = importlib.import_module("torch")
            transformers = importlib.import_module("transformers")
        except ImportError:
            raise ModelError("local Qwen models require the optional [models] dependencies") from None
        try:
            tokenizer = transformers.AutoTokenizer.from_pretrained(
                str(self.model_path), padding_side="left", local_files_only=True, trust_remote_code=False,
            )
            model_class = transformers.AutoModelForCausalLM if causal else transformers.AutoModel
            model = model_class.from_pretrained(
                str(self.model_path), local_files_only=True, trust_remote_code=False,
                torch_dtype=getattr(torch, self.dtype),
            ).to(self.device).eval()
        except Exception:
            # Model errors may contain prompts, credentials or absolute paths.
            raise ModelError("failed to load the local Qwen model; check files, dependencies and device") from None
        if getattr(model.config, "model_type", None) != "qwen3":
            raise ModelError("the configured local model must use the Qwen3 architecture")
        if self.max_length > getattr(model.config, "max_position_embeddings", 0):
            raise ModelError("max_length exceeds the model context limit")
        self._torch, self._tokenizer, self._model = torch, tokenizer, model

    async def _run(self, function: Any, *args: Any) -> Any:
        # Reject overload instead of queuing unbounded threads or GPU work.
        if not self._busy.acquire(blocking=False):
            raise ModelError("local model is busy; retry later")

        def guarded() -> Any:
            try:
                return function(*args)
            except ModelError:
                raise
            except Exception:
                raise ModelError("local model inference failed") from None
            finally:
                self._busy.release()

        task = asyncio.create_task(asyncio.to_thread(guarded))
        # A timeout cannot kill a Python/GPU call safely. Keep the busy lock until
        # the actual worker ends; do not launch overlapping retries on the device.
        def consume_result(done: asyncio.Task[Any]) -> None:
            if not done.cancelled():
                done.exception()

        task.add_done_callback(consume_result)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout)
        except TimeoutError:
            raise ModelError("local inference timed out; worker remains busy until it stops") from None


class Qwen3Embedder(_LocalQwen, Embedder):
    def __init__(
        self, model_path: str | Path, *, model_id: str = "Qwen/Qwen3-Embedding-0.6B",
        dimensions: int = 1024, instruction: str = QUERY_INSTRUCTION,
        model_revision: str = "", **kwargs: Any,
    ) -> None:
        super().__init__(model_path, model_id=model_id, **kwargs)
        self.dimensions = _integer(dimensions, "dimensions", 32, 4096)
        self.instruction = _instruction(instruction)
        self.model_revision = _revision(model_revision)

    @property
    def cache_identity(self) -> str:
        """Index/cache identity, separate from a provider's model routing name.

        Replacing weights in the same directory requires a new model_revision.
        No weight download or expensive full-weight hashing occurs here.
        """
        return _embedding_identity({
            "adapter": "qwen3-last-token-mrl-l2-v1", "model_id": self.model_id,
            "model_revision": self.model_revision, "model_path": str(self.model_path),
            "dimensions": self.dimensions, "instruction": self.instruction,
            "max_length": self.max_length, "dtype": self.dtype,
        })

    async def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        items = validate_texts(texts)
        if not items:
            return []
        if query:
            items = [query_instruction(text, self.instruction) for text in items]
        validate_texts(items)
        return await self._run(self._embed_sync, items)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        self._load(causal=False)
        if self.dimensions > getattr(self._model.config, "hidden_size", 0):
            raise ModelError("requested embedding dimensions exceed model output")
        vectors = []
        with self._torch.inference_mode():
            for offset in range(0, len(texts), self.batch_size):
                inputs = self._tokenizer(
                    texts[offset:offset + self.batch_size], padding=True, truncation=True,
                    max_length=self.max_length, return_tensors="pt",
                ).to(self.device)
                outputs = self._model(**inputs, use_cache=False)
                pooled = last_token_pool(outputs.last_hidden_state, inputs["attention_mask"])
                # MRL: slice requested dimensions before L2 normalization.
                pooled = self._torch.nn.functional.normalize(pooled[:, :self.dimensions].float(), p=2, dim=1)
                vectors.extend(pooled.cpu().tolist())
        return validate_embeddings(vectors, len(texts), self.dimensions)


class Qwen3Reranker(_LocalQwen, Reranker):
    def __init__(
        self, model_path: str | Path, *, model_id: str = "Qwen/Qwen3-Reranker-0.6B",
        instruction: str = QUERY_INSTRUCTION, **kwargs: Any,
    ) -> None:
        super().__init__(model_path, model_id=model_id, **kwargs)
        self.instruction = _instruction(instruction)

    async def score(self, query: str, documents: Sequence[str]) -> list[float]:
        validate_texts([query])
        items = validate_texts(documents)
        if not items:
            return []
        validate_texts([rerank_pair(query, document, self.instruction) for document in items])
        return await self._run(self._score_sync, query, items)

    def _score_sync(self, query: str, documents: list[str]) -> list[float]:
        self._load(causal=True)
        tokenizer = self._tokenizer
        prefix = tokenizer.encode(RERANK_PREFIX, add_special_tokens=False)
        suffix = tokenizer.encode(RERANK_SUFFIX, add_special_tokens=False)
        body_length = self.max_length - len(prefix) - len(suffix)
        if body_length <= 0:
            raise ModelError("max_length is too small for the Qwen reranker template")
        yes = tokenizer.encode("yes", add_special_tokens=False)
        no = tokenizer.encode("no", add_special_tokens=False)
        if len(yes) != 1 or len(no) != 1 or yes == no:
            raise ModelError("Qwen reranker requires distinct single-token yes/no labels")
        result = []
        with self._torch.inference_mode():
            for offset in range(0, len(documents), self.batch_size):
                pairs = [rerank_pair(query, doc, self.instruction)
                         for doc in documents[offset:offset + self.batch_size]]
                tokens = tokenizer(
                    pairs, padding=False, truncation=True, max_length=body_length,
                    add_special_tokens=False, return_attention_mask=False,
                )
                tokens["input_ids"] = [prefix + ids + suffix for ids in tokens["input_ids"]]
                inputs = tokenizer.pad(tokens, padding=True, return_tensors="pt").to(self.device)
                # Qwen3 in transformers>=4.51 supports last-token-only logits.
                # Avoid allocating batch * sequence * vocabulary logits or KV cache.
                logits = self._model(**inputs, logits_to_keep=1, use_cache=False).logits[:, -1, :].float()
                scores = self._torch.softmax(logits[:, [no[0], yes[0]]], dim=-1)[:, 1]
                result.extend(scores.cpu().tolist())
        return validate_scores(result, len(documents))


class _HTTPModel:
    def __init__(
        self, base_url: str, *, model_id: str, allow_online: bool = False,
        timeout: float = 30, api_key: str = "", transport: Any = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ModelError("model base_url must be an HTTP(S) URL without credentials/query/fragment")
        try:
            parsed.port
        except ValueError:
            raise ModelError("model base_url has an invalid port") from None
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            # Only a literal localhost name is trusted without egress opt-in.
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback and not allow_online:
            raise ModelError("non-loopback model access requires explicit allow_online=true, including LAN")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ModelError("model_id is required")
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.timeout = _positive_timeout(timeout)
        self.api_key = api_key
        self._transport = transport

    async def _post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._request(endpoint, body), timeout=self.timeout)
        except TimeoutError:
            raise ModelError("model HTTP request timed out") from None

    async def _request(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False, trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream("POST", self.base_url + endpoint, json=body, headers=headers) as response:
                    if response.status_code != 200:
                        raise ModelError(f"model HTTP request failed with status {response.status_code}")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ModelError("model response exceeds size limit")
                        data.extend(chunk)
                    payload = json.loads(data)
        except ModelError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise ModelError("model HTTP request failed or returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise ModelError("model response must be an object")
        if payload.get("model", self.model_id) != self.model_id:
            raise ModelError("model response identity mismatch")
        return payload


class HTTPEmbedder(_HTTPModel, Embedder):
    """OpenAI-shaped embedding endpoint; query instructions are applied once here."""

    def __init__(self, base_url: str, *, dimensions: int = 1024,
                 instruction: str = QUERY_INSTRUCTION, model_revision: str = "", **kwargs: Any) -> None:
        super().__init__(base_url, **kwargs)
        self.dimensions = _integer(dimensions, "dimensions", 32, 4096)
        self.instruction = _instruction(instruction)
        self.model_revision = _revision(model_revision)

    @property
    def cache_identity(self) -> str:
        """Server weight/tokenizer/truncation changes require a new revision.

        Credentials and transport-only settings never become cache metadata.
        """
        return _embedding_identity({
            "adapter": "http-embedding-mrl-l2-v1", "base_url": self.base_url,
            "model_id": self.model_id, "model_revision": self.model_revision,
            "dimensions": self.dimensions, "instruction": self.instruction,
        })

    async def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        items = validate_texts(texts)
        if not items:
            return []
        inputs = [query_instruction(text, self.instruction) for text in items] if query else items
        validate_texts(inputs)
        payload = await self._post("/v1/embeddings", {
            "model": self.model_id, "input": inputs, "dimensions": self.dimensions, "encoding_format": "float",
        })
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(items):
            raise ModelError("embedding response must contain one result per input")
        ordered: list[Any] = [None] * len(items)
        for item in data:
            if not isinstance(item, dict):
                raise ModelError("invalid embedding result")
            index = _integer(item.get("index"), "embedding index", 0, len(items) - 1)
            if ordered[index] is not None:
                raise ModelError("duplicate embedding index")
            ordered[index] = item.get("embedding")
        return validate_embeddings(ordered, len(items), self.dimensions)


class HTTPReranker(_HTTPModel, Reranker):
    async def score(self, query: str, documents: Sequence[str]) -> list[float]:
        validate_texts([query])
        items = validate_texts(documents)
        if not items:
            return []
        validate_texts([query + "\n" + document for document in items])
        payload = await self._post("/v1/rerank", {
            "model": self.model_id, "query": query, "documents": items, "top_n": len(items),
        })
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(items):
            raise ModelError("rerank response must contain one result per input")
        scores: list[Any] = [None] * len(items)
        for item in results:
            if not isinstance(item, dict):
                raise ModelError("invalid rerank result")
            index = _integer(item.get("index"), "rerank index", 0, len(items) - 1)
            if scores[index] is not None:
                raise ModelError("duplicate rerank index")
            scores[index] = item.get("relevance_score")
        return validate_scores(scores, len(items))
