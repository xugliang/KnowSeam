import hashlib
import json
from pathlib import PurePath

from ..domain import DocumentParser
from ..parsers import FEISHU_NATIVE_TYPE, preflight


class RoutingDocumentParser(DocumentParser):
    def __init__(self, native, enhanced, routes, *, max_bytes=30*1024*1024):
        self.native, self.enhanced = native, enhanced
        self.routes, self.max_bytes = dict(routes), max_bytes
        value = {'native': getattr(native, 'version', 'structured-v1'),
                 'enhanced': enhanced.version, 'routes': self.routes}
        self.version = 'router-' + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def parse(self, content):
        if content.media_type == FEISHU_NATIVE_TYPE:
            return self.native.parse(content)
        preflight(content, max_bytes=self.max_bytes)
        extension = PurePath(content.filename).suffix.lower().lstrip('.')
        parser = self.enhanced if self.routes.get(extension, 'native') == 'docling' else self.native
        return parser.parse(content)
