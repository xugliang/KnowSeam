"""Recoverable parsing subprocess. Container, not this process, supplies the sandbox."""
from abc import ABC, abstractmethod
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from ..domain import ParsedDocument, SourceContent, UnsupportedDocument
from .protocol import MAX_RESPONSE_BYTES, DoclingOptions, ParseMetadata, decode_document, encode_metadata


class ParseRunner(ABC):
    @abstractmethod
    def run(self, content: SourceContent, options: DoclingOptions) -> ParsedDocument: ...


class ProcessRunner(ParseRunner):
    def __init__(self, model_path: Path, *, command=None, max_response_bytes=MAX_RESPONSE_BYTES):
        self.model_path = model_path
        self.max_response_bytes = max_response_bytes
        self.command = command or (sys.executable, '-m', 'knowseam.document_parsing.process_runner',
                                   '--child', str(model_path))

    def run(self, content, options):
        metadata = ParseMetadata(filename=content.filename, media_type=content.media_type, options=options)
        environment = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'HOME': '/tmp',
                       'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
                       'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                       'OMP_NUM_THREADS': '2', 'MKL_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '2'}
        with tempfile.TemporaryFile() as input_file, tempfile.TemporaryFile() as output_file:
            input_file.write(encode_metadata(metadata).encode() + b'\n' + content.data)
            input_file.seek(0)
            process = subprocess.Popen(self.command, stdin=input_file, stdout=output_file,
                                       stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
            try:
                process.wait(timeout=options.timeout_seconds)
            except subprocess.TimeoutExpired:
                # Group is created by this task, never kill other workloads.
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise UnsupportedDocument('parser_timeout') from None
            if process.returncode != 0:
                raise UnsupportedDocument('parser_execution_failed')
            output_file.seek(0)
            body = output_file.read(self.max_response_bytes + 1)
            if len(body) > self.max_response_bytes:
                raise UnsupportedDocument('parser_response_limit')
            return decode_document(body, max_sections=options.max_sections)


def child(model_path):
    from .docling_adapter import DoclingDocumentParser
    from .executor import load_identity
    from .protocol import MAX_BYTES, decode_metadata, encode_document
    metadata = decode_metadata(sys.stdin.buffer.readline(8194).decode().strip())
    body = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise UnsupportedDocument('parser_input_limit')
    parser = DoclingDocumentParser(Path(model_path), load_identity(Path(model_path)))
    result = parser.parse_with_options(SourceContent(body, metadata.filename, metadata.media_type), metadata.options)
    sys.stdout.buffer.write(encode_document(result))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--child':
        child(sys.argv[2])
    else:
        raise SystemExit(2)
