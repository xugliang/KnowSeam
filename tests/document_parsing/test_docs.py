import json
from pathlib import Path

from knowseam.config import load_config
from knowseam.document_parsing.factory import build_parser
from knowseam.domain import SourceContent

ROOT = Path(__file__).resolve().parents[2]


def test_public_example_keeps_enhanced_parser_optional(tmp_path):
    values = json.loads((ROOT/'config.example.json').read_text())
    assert values['parsing']['docling']['enabled'] is False
    values['environment'] = 'test'
    values['file_root'] = str(tmp_path/'data')
    values['clients'][0]['token'] = 'example-test-only-ABCDEFGHIJKLMNOPQRSTUVWXYZ-1234567890'
    values['sources'] = []
    path = tmp_path/'config.json'
    path.write_text(json.dumps(values))
    settings = load_config(path)
    parser = build_parser(settings.parsing, max_bytes=30*1024*1024)
    assert parser.parse(SourceContent(b'example', 'example.txt', 'text/plain')).sections[0].text == 'example'
