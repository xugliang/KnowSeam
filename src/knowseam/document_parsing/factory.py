from ..parsers import StructuredDocumentParser
from .config import ParsingConfig


def build_parser(settings: ParsingConfig, *, max_bytes: int, client=None, structured_tables=False):
    native = StructuredDocumentParser(max_bytes=max_bytes, structured_tables=structured_tables)
    if not settings.docling.enabled:
        return native
    from .docling_adapter import DoclingAdapter
    from .router import RoutingDocumentParser
    if client is None:
        raise ValueError('enabled parser requires lifecycle-owned Unix client')
    enhanced = DoclingAdapter(client, settings.docling.options())
    return RoutingDocumentParser(native, enhanced, settings.routes, max_bytes=max_bytes)
