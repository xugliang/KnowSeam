import io
import pytest
from knowseam.chunking import digest
from knowseam.domain import SourceContent, UnsupportedDocument


def png(size=(4, 3), color="red"):
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def test_image_bomb_and_hash_mismatch_rejected():
    from knowseam.visual.assets import render_assets
    from knowseam.visual.domain import AssetRequest

    data = png()
    request = AssetRequest(source_sha256=digest(data), media_type="image/png")
    assets = render_assets(data, [request])
    assert len(assets) == 1 and assets[0].width == 4 and assets[0].height == 3
    with pytest.raises(ValueError, match="hash"):
        render_assets(png(color="blue"), [request])
    bomb = png((4001, 4000))
    with pytest.raises(ValueError, match="pixel"):
        render_assets(bomb, [AssetRequest(source_sha256=digest(bomb), media_type="image/png")])


def test_image_only_parse_is_explicit_and_hashes_visual_changes():
    from knowseam.visual.assets import VisualParser
    from knowseam.parsers import StructuredDocumentParser
    from knowseam.chunking import parsed_hash

    source = SourceContent(png(), "figure.png", "image/png")
    with pytest.raises(UnsupportedDocument):
        StructuredDocumentParser().parse(source)
    parser = VisualParser(StructuredDocumentParser())
    first = parser.parse(source)
    assert first.sections == () and len(first.assets) == 1 and first.structure_version == 2
    second = parser.parse(SourceContent(png(color="blue"), "figure.png", "image/png"))
    assert parsed_hash(first) != parsed_hash(second)


def test_parser_v2_visual_assets_require_explicit_negotiation():
    from knowseam.document_parsing.protocol import encode_document, decode_document
    from knowseam.visual.assets import VisualParser
    from knowseam.parsers import StructuredDocumentParser

    parsed = VisualParser(StructuredDocumentParser()).parse(SourceContent(png(), "image.png", "image/png"))
    body = encode_document(parsed, protocol_version=2)
    with pytest.raises(UnsupportedDocument):
        decode_document(body, protocol_version=2)
    value = decode_document(body, protocol_version=2, allow_assets=True)
    assert value.sections == () and value.assets == parsed.assets
