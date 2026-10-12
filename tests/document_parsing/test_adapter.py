import pytest

from knowseam.domain import ParsedDocument, Section, SourceContent, UnsupportedDocument


def test_restart_identity_change_never_publishes_old_pipeline():
    from knowseam.document_parsing.protocol import ParserIdentity, DoclingOptions
    from knowseam.document_parsing.docling_adapter import DoclingAdapter
    class Client:
        identity = ParserIdentity(upstream_sha='a'*40, runtime_source_sha256='b'*64,
                                  dependency_fingerprint='c'*64, model_fingerprint='d'*64)
        calls = 0
        def status(self):
            return self.identity
        def parse(self, content, options):
            self.calls += 1
            from knowseam.document_parsing.fingerprint import parser_fingerprint
            return ParsedDocument((Section('ok'),), parser_version=parser_fingerprint(
                self.identity, options, normalizer_version=self.identity.normalizer_version))
    client = Client()
    parser = DoclingAdapter(client, DoclingOptions())
    assert parser.parse(SourceContent(b'x', 'a.txt', 'text/plain')).sections[0].text == 'ok'
    client.identity = client.identity.model_copy(update={'runtime_source_sha256': 'e'*64})
    with pytest.raises(UnsupportedDocument, match='identity_changed'):
        parser.parse(SourceContent(b'x', 'a.txt', 'text/plain'))
    assert client.calls == 1


def test_restart_between_status_and_parse_rejects_mismatched_output():
    from knowseam.document_parsing.protocol import ParserIdentity, DoclingOptions
    from knowseam.document_parsing.docling_adapter import DoclingAdapter
    class Client:
        def status(self):
            return ParserIdentity(upstream_sha='a'*40, runtime_source_sha256='b'*64,
                                  dependency_fingerprint='c'*64, model_fingerprint='d'*64)
        def parse(self, content, options):
            return ParsedDocument((Section('new runtime'),), parser_version='different-pipeline')
    with pytest.raises(UnsupportedDocument, match='output_identity_changed'):
        DoclingAdapter(Client(), DoclingOptions()).parse(SourceContent(b'x', 'a.txt', 'text/plain'))
