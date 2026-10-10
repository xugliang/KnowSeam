"""Explicit maintainer-only download; runtime uses these verified bytes offline."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def make_models_readable(target):
    # Only this new, verified public-model directory is touched. Containers
    # run as a different UID and must traverse directories copied from SSH.
    target.chmod(0o755)
    for path in target.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--import-from', type=Path, help='offline prepared directory with verified manifest.json')
    args = parser.parse_args()
    target = args.directory.resolve()
    if target == Path('/') or target == Path.home().resolve() or target.exists():
        raise ValueError('dedicated NEW directory required; existing models not overwritten')
    if args.import_from:
        # Maintainer-only offline transfer, never exposed by the parsing API.
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
        from knowseam.document_parsing.fingerprint import model_manifest_hash
        source = args.import_from.resolve()
        if target.is_relative_to(source) or source.is_relative_to(target):
            raise ValueError('model import directories must not overlap')
        model_manifest_hash(source, verify=True)
        shutil.copytree(source, target, symlinks=False)
        model_manifest_hash(target, verify=True)
        make_models_readable(target)
        print('Imported verified model directory offline.')
        return
    from docling.datamodel.pipeline_options import LayoutObjectDetectionOptions
    from docling.models.stages.ocr import rapid_ocr_model
    from huggingface_hub import HfApi, snapshot_download
    target.mkdir(parents=True)
    # Resolve each repository revision before the explicit download. Selected
    # model implementations use these same repositories; manifest hashes are
    # the final authoritative identity even if upstream updates during fetch.
    api = HfApi()
    sources = []
    layout = LayoutObjectDetectionOptions().model_spec
    for name, revision, patterns in ((layout.repo_id, layout.revision, None),
            ('docling-project/docling-models', 'v2.3.0', ['model_artifacts/tableformer/**', 'README.md', 'LICENSE*'])):
        info = api.model_info(name, revision=revision)
        sources.append({'repo': name, 'revision': info.sha,
                        'license': (info.card_data or {}).get('license', 'review-required')})
        snapshot_download(name, revision=info.sha, local_dir=target/name.replace('/', '--'),
                          allow_patterns=patterns)
    urls = []
    original_download = rapid_ocr_model.download_url_with_progress
    def record_download(url, **kwargs):
        urls.append(url)
        return original_download(url, **kwargs)
    rapid_ocr_model.download_url_with_progress = record_download
    try:
        rapid_ocr_model.RapidOcrModel.download_models(backend='onnxruntime', lang='ch',
                                                     model_size='small', local_dir=target/'RapidOcr')
    finally:
        rapid_ocr_model.download_url_with_progress = original_download
    from importlib.metadata import version
    sources.append({'name': 'RapidOCR', 'urls': urls, 'package_version': version('rapidocr'),
                    'license': 'review-required'})
    # Dereference cache links into this project directory, never mount arbitrary
    # cache locations into the isolated runtime.
    for file in target.rglob('*'):
        if file.is_symlink() and file.is_file():
            temporary = file.with_name(file.name + '.copy')
            shutil.copyfile(file, temporary)
            file.unlink()
            temporary.replace(file)
    files = {}
    for file in sorted(target.rglob('*')):
        if file.is_file():
            value = hashlib.sha256()
            with file.open('rb') as stream:
                for part in iter(lambda: stream.read(1024*1024), b''):
                    value.update(part)
            files[file.relative_to(target).as_posix()] = value.hexdigest()
    if not files:
        raise ValueError('model download produced no files')
    manifest = {'files': files, 'sources': sources, 'ocr': 'onnxruntime:ch:small',
                'license_review_required': any(source['license'] == 'review-required' for source in sources)}
    (target/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    make_models_readable(target)
    print('Prepared model files:', len(files), '; review license manifest before distribution.')


if __name__ == '__main__':
    main()
