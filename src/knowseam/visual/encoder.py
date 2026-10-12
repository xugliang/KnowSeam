"""Query/API processes only use this Unix-domain-socket client, never load a model."""

import base64
import httpx

from ..document_parsing.protocol import load_json
from .domain import MultiVector, VisualEncoder


class VisualClient(VisualEncoder):
    def __init__(self, config, *, transport=None):
        self.config, self.fingerprint = config, config.fingerprint
        self.client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(uds=str(config.socket_path)),
            base_url="http://visual",
            timeout=config.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )

    async def _encode(self, body):
        async with self.client.stream(
            "POST", "/v1/encode", json={"fingerprint": self.fingerprint, **body}
        ) as response:
            if response.status_code != 200:
                raise ValueError("visual_runtime_unavailable")
            data = bytearray()
            async for part in response.aiter_bytes():
                if len(data) + len(part) > 8 * 1024 * 1024:
                    raise ValueError("visual_response_limit")
                data.extend(part)
        result = load_json(bytes(data))
        if not isinstance(result, dict) or result.get("fingerprint") != self.fingerprint:
            raise ValueError("visual_fingerprint_mismatch")
        if (
            set(result) != {"fingerprint", "vectors"}
            or not isinstance(result["vectors"], list)
            or len(result["vectors"]) != 1
        ):
            raise ValueError("visual_vector_count")
        vectors = [MultiVector.model_validate(v) for v in result["vectors"]]
        if any(v.dimensions != self.config.dimensions for v in vectors):
            raise ValueError("visual_dimension_mismatch")
        return vectors

    async def ready(self):
        try:
            async with self.client.stream("GET", "/v1/status", timeout=2) as response:
                if response.status_code != 200:
                    return False
                data = bytearray()
                async for part in response.aiter_bytes():
                    if len(data) + len(part) > 4096:
                        return False
                    data.extend(part)
            result = load_json(bytes(data))
            return (isinstance(result, dict) and result.get("ready") is True
                    and result.get("fingerprint") == self.fingerprint)
        except (httpx.HTTPError, ValueError, TypeError):
            return False

    async def encode_queries(self, texts):
        if len(texts) != 1 or not isinstance(texts[0], str) or not texts[0].strip() or len(texts[0]) > 4096:
            raise ValueError("visual_query_limit")
        return await self._encode({"query": texts[0]})

    async def encode_assets(self, assets):
        if len(assets) != 1 or len(assets[0].data) > 10 * 1024 * 1024:
            raise ValueError("visual_batch_limit")
        return await self._encode({"image": base64.b64encode(assets[0].data).decode()})

    async def close(self):
        await self.client.aclose()
