from pathlib import Path

import httpx

from ..domain import SourceContent, UnsupportedDocument
from .protocol import (HEADER, MAX_BYTES, MAX_RESPONSE_BYTES, DoclingOptions, ParseMetadata,
                       ParserIdentity, decode_document, encode_metadata, load_json)


class ParserClient:
    def __init__(self, socket_path: Path, *, timeout_seconds: int = 120, max_bytes: int = MAX_BYTES):
        self.max_bytes = min(max_bytes, MAX_BYTES)
        self.client = httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path), retries=0),
                                   base_url='http://parser', timeout=timeout_seconds + 5,
                                   follow_redirects=False, trust_env=False)

    def _request(self, method, path, *, limit, **kwargs):
        try:
            with self.client.stream(method, path, **kwargs) as response:
                if response.status_code != 200:
                    raise UnsupportedDocument(f'parser_http_{response.status_code}')
                result = bytearray()
                for part in response.iter_bytes():
                    if len(result) + len(part) > limit:
                        raise UnsupportedDocument('parser_response_limit')
                    result.extend(part)
                return bytes(result)
        except httpx.HTTPError:
            raise UnsupportedDocument('parser_unavailable') from None

    def status(self) -> ParserIdentity:
        try:
            return ParserIdentity.model_validate(load_json(self._request('GET', '/v1/status', limit=8192)))
        except (ValueError, TypeError):
            raise UnsupportedDocument('parser_identity_invalid') from None

    def parse(self, content: SourceContent, options: DoclingOptions):
        if len(content.data) > self.max_bytes:
            raise UnsupportedDocument('parser_input_limit')
        metadata = ParseMetadata(filename=content.filename, media_type=content.media_type, options=options)
        body = self._request('POST', '/v1/parse', limit=MAX_RESPONSE_BYTES, content=content.data,
                             headers={HEADER: encode_metadata(metadata)})
        return decode_document(body, max_sections=options.max_sections)

    def close(self):
        self.client.close()
