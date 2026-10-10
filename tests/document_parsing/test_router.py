import io
import zipfile

import pytest

from knowseam.domain import ParsedDocument, Section, SourceContent, UnsupportedDocument
from knowseam.parsers import FEISHU_NATIVE_TYPE, StructuredDocumentParser


def pdf_bytes():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_disabled_factory_needs_no_enhanced_client():
    from knowseam.document_parsing.factory import build_parser
    from knowseam.document_parsing.config import ParsingConfig
    parser = build_parser(ParsingConfig(), max_bytes=30 * 1024 * 1024)
    assert isinstance(parser, StructuredDocumentParser)
    assert parser.parse(SourceContent('通用资料'.encode(), '说明.txt', 'text/plain')).sections[0].text == '通用资料'


def test_feishu_priority_and_no_silent_fallback():
    from knowseam.document_parsing.router import RoutingDocumentParser
    class Enhanced:
        version = 'test'
        def parse(self, content):
            raise UnsupportedDocument('enhanced failure')
    parser = RoutingDocumentParser(StructuredDocumentParser(), Enhanced(), {'pdf': 'docling'})
    data = b'{"kind":"sheet","sheets":[{"id":"one","title":"Sheet","rows":[{"row":1,"values":["test"]}]}]}'
    assert parser.parse(SourceContent(data, 'renamed.pdf', FEISHU_NATIVE_TYPE)).sections[0].text == 'test'
    with pytest.raises(UnsupportedDocument, match='enhanced failure'):
        parser.parse(SourceContent(pdf_bytes(), '说明.pdf', 'application/pdf'))


def test_macro_checked_before_enhanced():
    from knowseam.document_parsing.router import RoutingDocumentParser
    class Enhanced:
        version = 'test'
        def parse(self, content):
            pytest.fail('malicious zip must not reach enhanced engine')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('[Content_Types].xml', 'test')
        archive.writestr('word/vbaProject.bin', b'macro')
    parser = RoutingDocumentParser(StructuredDocumentParser(), Enhanced(), {'docx': 'docling'})
    with pytest.raises(UnsupportedDocument):
        parser.parse(SourceContent(stream.getvalue(), '说明.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'))


def test_unicode_name_and_mismatched_format():
    from knowseam.document_parsing.router import RoutingDocumentParser
    class Enhanced:
        version = 'test'
        def parse(self, content):
            return ParsedDocument((Section(content.filename),))
    parser = RoutingDocumentParser(StructuredDocumentParser(), Enhanced(), {'pdf': 'docling'})
    assert parser.parse(SourceContent(pdf_bytes(), '中文.pdf', 'application/pdf')).sections[0].text == '中文.pdf'
    for data, media in ((b'<html>not pdf', 'application/pdf'), (b'%PDF-', 'image/png')):
        with pytest.raises(UnsupportedDocument):
            parser.parse(SourceContent(data, '中文.pdf', media))
