"""Renderer boundary: trusted archived bytes only, no user paths or network."""

from dataclasses import replace
import io
from pathlib import PurePath
import warnings

from ..chunking import digest
from ..domain import DocumentParser, ParsedDocument, UnsupportedDocument
from ..parsers import preflight
from .domain import AssetRequest, VisualAsset

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 16000000


class VisualParser(DocumentParser):
    def __init__(self, parser):
        self.parser = parser
        self.version = getattr(parser, "version", "native") + ":visual-source-v1"

    def parse(self, content):
        media = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }.get(PurePath(content.filename).suffix.lower())
        if media is None:
            return self.parser.parse(content)
        preflight(content)
        try:
            parsed = self.parser.parse(content)
        except UnsupportedDocument:
            parsed = ParsedDocument((), ("text_unavailable_visual_pending",))
        request = AssetRequest(
            source_sha256=digest(content.data),
            media_type=media,
            kind="page" if media == "application/pdf" else "image",
        )
        regions = [AssetRequest.model_validate(a) for a in parsed.assets]
        if any(a.source_sha256 != request.source_sha256 or a.media_type != media for a in regions):
            raise ValueError("visual_region_source_mismatch")
        return replace(
            parsed, assets=tuple(a.model_dump(mode="json") for a in [request, *regions]), structure_version=2
        )


def _image(data):
    from PIL import Image

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            image.close()
            raise ValueError("visual_pixel_limit")
        if getattr(image, "n_frames", 1) != 1:
            image.close()
            raise ValueError("visual_multiframe_unsupported")
        image.load()
        result = image.convert("RGB")
        image.close()
        return result


def render_assets(data, requests):
    if not data or len(data) > 30 * 1024 * 1024 or not 1 <= len(requests) <= 500:
        raise ValueError("visual_source_limit")
    requests = [AssetRequest.model_validate(r) for r in requests]
    if any(r.source_sha256 != digest(data) for r in requests):
        raise ValueError("visual_source_hash_mismatch")
    if len({r.media_type for r in requests}) != 1:
        raise ValueError("visual_source_type_mismatch")
    pdf = None
    from PIL import Image

    if requests[0].media_type == "application/pdf":
        import pypdfium2 as pdfium

        if not data.startswith(b"%PDF-"):
            raise ValueError("visual_pdf_signature")
        pdf = pdfium.PdfDocument(data)
        count = len(pdf)
        if not 1 <= count <= 500:
            pdf.close()
            raise ValueError("visual_page_limit")
    else:
        count = 1
    result = []
    try:
        for request in requests:
            for number in [request.page] if request.page else range(1, count + 1):
                if number > count or len(result) >= 500:
                    raise ValueError("visual_page_limit")
                if pdf is not None:
                    page = pdf[number - 1]
                    bitmap = None
                    try:
                        width, height = page.get_size()
                        if width <= 0 or height <= 0:
                            raise ValueError("visual_page_dimensions")
                        bitmap = page.render(scale=min(2.0, 2048 / max(width, height)))
                        image = bitmap.to_pil().convert("RGB")
                    finally:
                        if bitmap is not None:
                            bitmap.close()
                        page.close()
                else:
                    image = _image(data)
                try:
                    if request.bbox:
                        x, y, u, v = request.bbox
                        cropped = image.crop(
                            (
                                int(x * image.width),
                                int(y * image.height),
                                max(int(u * image.width), 1),
                                max(int(v * image.height), 1),
                            )
                        )
                        image.close()
                        image = cropped
                    if image.width < 1 or image.height < 1 or image.width * image.height > MAX_PIXELS:
                        raise ValueError("visual_pixel_limit")
                    image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                    output = io.BytesIO()
                    image.save(output, format="PNG")
                    value = output.getvalue()
                    if len(value) > MAX_BYTES:
                        raise ValueError("visual_asset_byte_limit")
                    result.append(
                        VisualAsset(
                            value,
                            image.width,
                            image.height,
                            number,
                            request.kind,
                            request.bbox,
                            request.table_id,
                        )
                    )
                    if sum(len(a.data) for a in result) > 128 * 1024 * 1024:
                        raise ValueError("visual_document_asset_byte_limit")
                finally:
                    image.close()
    finally:
        if pdf is not None:
            pdf.close()
    return result
