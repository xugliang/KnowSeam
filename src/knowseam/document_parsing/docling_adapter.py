"""Heavy imports are confined to the isolated executor, never the worker adapter."""
from pathlib import Path

from ..domain import DocumentParser, SourceContent, UnsupportedDocument
from ..parsers import preflight
from .config import DoclingOptions
from .fingerprint import parser_fingerprint
from .normalizer import NORMALIZER_VERSION, normalize_document


class DoclingAdapter(DocumentParser):
    def __init__(self, client, options):
        self.client, self.options = client, options
        self.identity = client.status()
        self.version = parser_fingerprint(self.identity, options, normalizer_version=NORMALIZER_VERSION)

    def parse(self, content):
        # A restarted parser must not publish new output under an old pipeline ID.
        if self.client.status() != self.identity:
            raise UnsupportedDocument('parser_identity_changed_restart_worker')
        parsed = self.client.parse(content, self.options)
        if parsed.parser_version != self.version:
            raise UnsupportedDocument('parser_output_identity_changed_restart_worker')
        return parsed


class DoclingDocumentParser(DocumentParser):
    def __init__(self, model_path: Path, identity):
        self.model_path, self.identity = model_path, identity

    def parse(self, content):
        return self.parse_with_options(content, DoclingOptions())

    def parse_with_options(self, content: SourceContent, options):
        import io
        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
        from docling.document_converter import DocumentConverter, PdfFormatOption, ImageFormatOption
        preflight(content)
        pipeline = PdfPipelineOptions(artifacts_path=self.model_path, do_ocr=options.ocr_enabled,
                                       do_table_structure=options.table_structure_enabled,
                                       enable_remote_services=False, allow_external_plugins=False,
                                       accelerator_options=AcceleratorOptions(
                                           device=AcceleratorDevice.CPU, num_threads=2))
        pipeline.ocr_options = RapidOcrOptions(backend='onnxruntime', lang=['ch'], model_size='small')
        converter = DocumentConverter(allowed_formats=[InputFormat.PDF, InputFormat.IMAGE,
                                      InputFormat.DOCX, InputFormat.XLSX, InputFormat.PPTX],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
                            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline)})
        result = converter.convert(DocumentStream(name=content.filename, stream=io.BytesIO(content.data)),
                                   max_num_pages=options.max_pages, raises_on_error=True)
        if getattr(result.status, 'value', result.status) != 'success':
            raise UnsupportedDocument('parser_incomplete_conversion')
        version = parser_fingerprint(self.identity, options, normalizer_version=NORMALIZER_VERSION)
        return normalize_document(result.document, parser_version=version, max_sections=options.max_sections,
                                  max_text_chars=150*1024*1024)
