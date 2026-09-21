"""Explicit private Feishu/HTTP container snapshot; never edit host configuration."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from knowseam.config import MAX_CONFIG_BYTES, Settings


class SnapshotError(ValueError):
    pass


def prepare(project_root: Path, *, replace=False):
    root = project_root.resolve(strict=True)
    source, target = root / "config.local.json", root / "config.container.json"
    if project_root.is_symlink() or root == Path(root.anchor) or source.is_symlink() or target.is_symlink():
        raise SnapshotError("dedicated regular paths required")
    if target.exists() and not replace:
        raise SnapshotError("container configuration already exists")
    with source.open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise SnapshotError("configuration exceeds size limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SnapshotError("duplicate JSON keys")
            result[key] = value
        return result
    values = json.loads(raw, object_pairs_hook=unique)
    settings = Settings.model_validate(values)
    if any(s.type != "feishu" for s in settings.sources):
        raise SnapshotError("filesystem needs explicit mount mapping")
    if any(m.backend != "http" for m in (settings.models.embedding, settings.models.reranker)):
        raise SnapshotError("local models need explicit mount mapping")
    if settings.tokenization.user_dictionary is not None:
        raise SnapshotError("user_dictionary needs explicit mount mapping")
    values["file_root"] = "/app/data"
    Settings.model_validate(values)
    descriptor, name = tempfile.mkstemp(prefix=".container-config-", dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            json.dump(values, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
        return {"status": "prepared", "file": "config.container.json"}
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.project, replace=args.replace)))
    except Exception:
        print("Container configuration could not be prepared; check private configuration.", file=sys.stderr)
        raise SystemExit(1) from None
