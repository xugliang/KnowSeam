"""Explicitly published local files, primarily for controlled imports and tests."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from ..domain import AccessDenied, NotFound, SourceConnector, SourceContent, SourceNode, SourceSnapshot
from ..permissions.domain import PermissionConnector, public_snapshot


class FilesystemConnector(SourceConnector, PermissionConnector):
    def __init__(self, root: str | Path, *, public_files: tuple[str, ...] = (),
                 max_download_bytes: int = 30 * 1024 * 1024):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("source root must be a directory")
        self.public_files = frozenset(public_files)
        self.max_download_bytes = max_download_bytes

    def _path(self, relative: str) -> Path:
        if relative not in self.public_files:
            raise AccessDenied("file is not explicitly published")
        candidate = self.root / relative
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise AccessDenied("file escapes published directory")
        # Symlink/junction publication is deliberately unsupported, including ancestors.
        for item in (candidate, *candidate.parents):
            if item == self.root:
                break
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                raise AccessDenied("published paths must not contain links")
        return resolved

    async def scan(self) -> SourceSnapshot:
        nodes, errors = [], []
        for relative in sorted(self.public_files):
            try:
                path = self._path(relative)
                if not path.exists():
                    continue  # A complete enumeration can now retire a deleted file.
                if not path.is_file():
                    raise AccessDenied("published entry is not a regular file")
                info = path.stat()
                if info.st_size > self.max_download_bytes:
                    # Keep the node visible: a size error is not deletion.
                    revision = f"oversized:{info.st_mtime_ns}:{info.st_size}"
                else:
                    revision = hashlib.sha256(path.read_bytes()).hexdigest()
                nodes.append(SourceNode(relative, path.name, revision,
                                        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                                        path.as_uri(), True, {"filename": path.name}))
            except (OSError, AccessDenied) as exc:
                errors.append(f"{relative}: {type(exc).__name__}")
        return SourceSnapshot(tuple(nodes), not errors, tuple(errors))

    async def fetch_permissions(self, external_ids):
        import asyncio
        def check(relative):
            try:
                return public_snapshot(relative, self._path(relative).is_file())
            except (OSError, AccessDenied):
                return public_snapshot(relative, False)
        return tuple(await asyncio.gather(*(asyncio.to_thread(check, value) for value in external_ids)))

    async def fetch(self, node: SourceNode) -> SourceContent:
        if node.public is not True:
            raise AccessDenied("publication has not been verified")
        path = self._path(node.external_id)
        try:
            with path.open("rb") as stream:
                data = stream.read(self.max_download_bytes + 1)
        except FileNotFoundError as exc:
            raise NotFound(node.external_id) from exc
        if len(data) > self.max_download_bytes:
            raise ValueError("source exceeds download limit")
        return SourceContent(data, path.name, node.media_type)
