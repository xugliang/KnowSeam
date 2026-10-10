"""Content-based identity, independent of deployment paths."""
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath

from .protocol import load_json


def parser_fingerprint(identity, options, *, normalizer_version):
    value = {'identity': identity.model_dump(), 'options': options.model_dump(), 'normalizer': normalizer_version}
    return 'docling-' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def installed_runtime_hash():
    spec = importlib.util.find_spec('docling')
    if spec is None or spec.origin is None:
        raise ValueError('Docling implementation missing')
    root = Path(spec.origin).parent.parent
    result = hashlib.sha256()
    for file in sorted((root/'docling').rglob('*')):
        if file.is_file() and '__pycache__' not in file.parts and file.suffix != '.pyc':
            result.update(file.relative_to(root).as_posix().encode() + b'\0' + hashlib.sha256(file.read_bytes()).digest())
    return result.hexdigest()


def model_manifest_hash(root: Path, *, verify=False):
    manifest = root / 'manifest.json'
    if not manifest.is_file() or manifest.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('parser model manifest missing or oversized')
    values = load_json(manifest.read_bytes())
    files = values['files']
    if not isinstance(files, dict) or not files or len(files) > 10000:
        raise ValueError('invalid model manifest')
    for name, expected in files.items():
        path = PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts or '\\' in name or name == 'manifest.json'
                or not isinstance(expected, str) or len(expected) != 64):
            raise ValueError('invalid model path/digest')
        target = root / name
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError('model path escapes root')
        if verify:
            if not target.is_file():
                raise ValueError('model file missing')
            digest = hashlib.sha256()
            with target.open('rb') as stream:
                for part in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(part)
            if digest.hexdigest() != expected:
                raise ValueError('model checksum mismatch')
    if verify:
        actual = {file.relative_to(root).as_posix() for file in root.rglob('*')
                  if file.is_file() and file != manifest}
        if actual != set(files):
            raise ValueError('model files differ from manifest')
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
