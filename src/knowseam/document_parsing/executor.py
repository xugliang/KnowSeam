"""Unix-only parsing service. Does not read knowledge configuration or credentials."""
import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
import threading

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..domain import SourceContent, UnsupportedDocument
from .protocol import (HEADER, MAX_BYTES, MAX_RESPONSE_BYTES, ParserIdentity, decode_document,
                       decode_metadata, encode_document, load_json)


@dataclass(frozen=True)
class ExecutorSettings:
    socket_path: Path
    model_path: Path
    identity: ParserIdentity
    max_bytes: int = MAX_BYTES
    timeout_seconds: int = 120
    max_pages: int = 200
    max_sections: int = 50000
    max_text_chars: int = MAX_RESPONSE_BYTES
    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_metadata_bytes: int = 8192


def create_executor_app(settings: ExecutorSettings, *, runner):
    busy = threading.Lock()

    async def status(request):
        return JSONResponse(settings.identity.model_dump())

    async def parse(request: Request):
        try:
            metadata = decode_metadata(request.headers.get(HEADER, ''), max_bytes=settings.max_metadata_bytes)
            options = metadata.options
            if (options.timeout_seconds > settings.timeout_seconds or options.max_pages > settings.max_pages
                    or options.max_sections > settings.max_sections):
                raise ValueError('request exceeds executor limits')
            body = bytearray()
            async for part in request.stream():
                if len(body) + len(part) > settings.max_bytes:
                    return JSONResponse({'error': 'parser_input_limit'}, status_code=413)
                body.extend(part)
        except (ValueError, TypeError, UnicodeError):
            return JSONResponse({'error': 'parser_invalid_request'}, status_code=400)
        if not body:
            return JSONResponse({'error': 'parser_empty_input'}, status_code=400)
        if not busy.acquire(blocking=False):
            return JSONResponse({'error': 'parser_busy'}, status_code=429)
        def run_and_release():
            try:
                return runner.run(SourceContent(bytes(body), metadata.filename, metadata.media_type), options)
            finally:
                busy.release()
        # Cancellation of the HTTP request must not make a still-running child
        # disappear from the concurrency accounting.
        running = asyncio.create_task(asyncio.to_thread(run_and_release))
        try:
            result = await asyncio.shield(running)
            output = encode_document(result)
            if len(output) > settings.max_response_bytes:
                raise UnsupportedDocument('parser_response_limit')
            decode_document(output, max_sections=options.max_sections, max_text_chars=settings.max_text_chars)
            return Response(output, media_type='application/json')
        except Exception as error:
            category = 'parser_timeout' if isinstance(error, UnsupportedDocument) and str(error) == 'parser_timeout' else 'parser_execution_failed'
            return JSONResponse({'error': category}, status_code=504 if category == 'parser_timeout' else 422)

    return Starlette(routes=[Route('/v1/status', status), Route('/v1/parse', parse, methods=['POST'])])


def validate_socket_target(path: Path):
    if not path.parent.is_dir():
        raise ValueError('socket parent missing')
    if path.is_symlink():
        raise ValueError('foreign socket symlink')
    if path.exists():
        import os
        import stat
        before = path.lstat()
        if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid():
            raise ValueError('foreign socket target')
        # /proc includes sockets already bound but not yet listening.
        active = Path('/proc/net/unix').read_text().splitlines()[1:]
        if any(line.split(maxsplit=7)[-1] == str(path) for line in active):
            raise ValueError('active socket target')
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_uid) != (after.st_dev, after.st_ino, after.st_uid):
            raise ValueError('socket target changed')
        path.unlink()  # Only verified own stale socket, never a directory or another UID.


def load_identity(model_path: Path) -> ParserIdentity:
    from .fingerprint import model_manifest_hash, installed_runtime_hash
    from .normalizer import NORMALIZER_VERSION
    build = load_json(Path('/app/parser-build.json').read_bytes())
    if installed_runtime_hash() != build['runtime_source_sha256']:
        raise ValueError('installed runtime differs from build identity')
    return ParserIdentity(upstream_sha=build['upstream_sha'],
                          runtime_source_sha256=build['runtime_source_sha256'],
                          dependency_fingerprint=build['dependency_fingerprint'],
                          model_fingerprint=model_manifest_hash(model_path, verify=True),
                          normalizer_version=NORMALIZER_VERSION)


def main():
    import os
    import uvicorn
    from .process_runner import ProcessRunner
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--model-path', type=Path, required=True)
    args = parser.parse_args()
    validate_socket_target(args.socket)
    identity = load_identity(args.model_path)
    settings = ExecutorSettings(args.socket, args.model_path, identity)
    os.umask(0o007)
    uvicorn.run(create_executor_app(settings, runner=ProcessRunner(args.model_path)),
                uds=str(args.socket), access_log=False, log_level='warning')


if __name__ == '__main__':
    main()
