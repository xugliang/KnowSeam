"""Verify local source/wheel identity. No imports of Docling or network calls."""
import argparse
import hashlib
import json
from pathlib import Path
import tomllib
import zipfile


def runtime_hash(root: Path) -> str:
    files = {path.relative_to(root).as_posix(): path.read_bytes()
             for path in (root / 'docling').rglob('*')
             if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
    if not files:
        raise ValueError('runtime source missing')
    return files_hash(files)


def files_hash(files: dict[str, bytes]) -> str:
    value = hashlib.sha256()
    for name, content in sorted(files.items()):
        value.update(name.encode('utf-8') + b'\0' + hashlib.sha256(content).digest())
    return value.hexdigest()


def validate_layout(root: Path) -> dict:
    values = tomllib.loads((root / 'pyproject.toml').read_text())
    project = values['project']
    if project['name'] != 'docling-slim' or not (root / 'docling/__init__.py').is_file():
        raise ValueError('expected implementation package docling-slim, not packaging facade')
    if values['tool']['hatch']['build']['targets']['wheel']['packages'] != ['docling']:
        raise ValueError('unsupported upstream wheel layout')
    return project


def verify_wheel(root: Path, wheel: Path) -> None:
    project = validate_layout(root)
    with zipfile.ZipFile(wheel) as archive:
        files = {name: archive.read(name) for name in archive.namelist()
                 if name.startswith('docling/') and not name.endswith('/')}
        if files_hash(files) != runtime_hash(root):
            raise ValueError('wheel differs from local runtime source')
        metadata = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(metadata) != 1:
            raise ValueError('invalid wheel metadata')
        text = archive.read(metadata[0]).decode()
        if f'Name: {project["name"]}\n' not in text or f'Version: {project["version"]}\n' not in text:
            raise ValueError('wheel package identity mismatch')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--wheel', type=Path)
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--lock', type=Path)
    args = parser.parse_args()
    project = validate_layout(args.root)
    if args.wheel:
        verify_wheel(args.root, args.wheel)
    result = {'runtime_source_sha256': runtime_hash(args.root),
              'distribution_name': project['name'], 'distribution_version': project['version']}
    if args.metadata:
        result = {**json.loads(args.metadata.read_text()), **result}
    if args.lock:
        result['dependency_fingerprint'] = hashlib.sha256(args.lock.read_bytes()).hexdigest()
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
