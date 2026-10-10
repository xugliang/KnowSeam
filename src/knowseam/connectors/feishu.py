"""Read-only Feishu Wiki connector with bounded I/O and fail-closed publication.

Public link permission is checked per underlying document, not inherited from its
Wiki parent. Tenant-wide read/edit links are accepted; bot-only ACL is not.
No document writes, conversion/import calls, message sends or online OCR occur.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
from collections import deque
from email.message import Message
from pathlib import PurePath
from urllib.parse import quote, urlparse

import httpx

from ..domain import (AccessDenied, IncompleteSnapshot, KnowledgeError, NotFound,
                      SourceConnector, SourceContent, SourceNode, SourceSnapshot, UnsupportedDocument)
from ..parsers import FEISHU_NATIVE_TYPE


class FeishuWikiConnector(SourceConnector):
    API_ORIGIN = "https://open.feishu.cn"
    PUBLIC_LINKS = frozenset({"tenant_readable", "tenant_editable"})

    def __init__(self, *, app_id: str, app_secret: str, root_token: str,
                 wiki_origin: str = "https://www.feishu.cn", transport=None,
                 timeout_seconds: float = 20, max_attempts: int = 3,
                 max_download_bytes: int = 30 * 1024 * 1024, max_nodes: int = 10000,
                 max_pages: int = 1000, max_sheet_cells: int = 2_000_000,
                 excluded_node_tokens=(), excluded_object_ids=(),
                 sleep=asyncio.sleep):
        if not app_id or not app_secret:
            raise ValueError("Feishu application credentials are required")
        self._check_token(root_token)
        origin = urlparse(wiki_origin)
        if (origin.scheme != "https" or not origin.hostname or
                not (origin.hostname == "feishu.cn" or origin.hostname.endswith(".feishu.cn")) or
                origin.username or origin.password or origin.port not in (None, 443) or
                origin.path not in ("", "/") or origin.query or origin.fragment):
            raise ValueError("wiki_origin must be an official Feishu HTTPS origin")
        if not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be 1..3")
        if min(timeout_seconds, max_download_bytes, max_nodes, max_pages, max_sheet_cells) <= 0:
            raise ValueError("limits must be positive")
        self.app_id, self.app_secret = app_id, app_secret
        self.root_token, self.wiki_origin = root_token, wiki_origin.rstrip("/")
        if (not isinstance(excluded_node_tokens, (tuple, list)) or len(excluded_node_tokens) > 1000
                or not isinstance(excluded_object_ids, (tuple, list)) or len(excluded_object_ids) > 1000):
            raise ValueError("invalid source exclusions")
        self.excluded_nodes = frozenset(self._check_token(token) for token in excluded_node_tokens)
        if any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_]+:[A-Za-z0-9_-]{1,200}", value)
               for value in excluded_object_ids):
            raise ValueError("invalid excluded object identifier")
        self.excluded_objects = frozenset(excluded_object_ids)
        self.max_attempts, self.max_download_bytes = max_attempts, max_download_bytes
        self.max_nodes, self.max_pages, self.max_sheet_cells = max_nodes, max_pages, max_sheet_cells
        self.client = httpx.AsyncClient(transport=transport, timeout=timeout_seconds,
                                       follow_redirects=False, trust_env=False)
        self.sleep = sleep
        self._access_token, self._token_until = "", 0.0
        self._token_lock = asyncio.Lock()

    @staticmethod
    def _check_token(token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", token):
            raise ValueError("invalid Feishu resource identifier")
        return token

    async def close(self):
        await self.client.aclose()

    async def _access(self):
        if self._access_token and time.monotonic() < self._token_until:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._token_until:
                return self._access_token
            result = await self._request("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                                         body={"app_id": self.app_id, "app_secret": self.app_secret}, auth=False)
            token = result.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise AccessDenied("Feishu did not issue an application token")
            self._access_token = token
            self._token_until = time.monotonic() + max(1, int(result.get("expire", 7200)) - 60)
            return token

    async def _request(self, method, path, *, params=None, body=None, auth=True, binary=False):
        # Resource APIs are GET-only. The only POST permitted is token acquisition.
        if method != "GET" and not (method == "POST" and path == "/open-apis/auth/v3/tenant_access_token/internal"):
            raise AccessDenied("connector resource writes are prohibited")
        if not path.startswith("/open-apis/") or ".." in path or "?" in path or "#" in path:
            raise AccessDenied("invalid API path")
        url = self.API_ORIGIN + path
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or parsed.port not in (None, 443):
            raise AccessDenied("only official Feishu API downloads are permitted")
        headers = {"Authorization": "Bearer " + await self._access()} if auth else {}
        limit = self.max_download_bytes if binary else min(self.max_download_bytes, 10 * 1024 * 1024)
        for attempt in range(self.max_attempts):
            retry_after = min(0.25 * 2 ** attempt, 2.0)
            try:
                async with self.client.stream(method, url, params=params, json=body, headers=headers) as response:
                    if response.status_code in (429, 500, 502, 503, 504):
                        try:
                            retry_after = min(max(float(response.headers.get("retry-after", retry_after)), 0), 5)
                        except ValueError:
                            pass
                        if attempt + 1 == self.max_attempts:
                            raise KnowledgeError(f"Feishu HTTP {response.status_code}; retry limit reached")
                    else:
                        if 300 <= response.status_code < 400:
                            # Never follow a redirect with credentials, including download redirects.
                            raise AccessDenied("Feishu redirect rejected; no credentials forwarded")
                        if response.status_code in (401, 403):
                            raise AccessDenied("Feishu denied resource access")
                        if response.status_code == 404:
                            raise NotFound("Feishu resource not found")
                        if response.status_code >= 400:
                            raise KnowledgeError(f"Feishu HTTP {response.status_code}")
                        length = response.headers.get("content-length", "")
                        if length.isdigit() and int(length) > limit:
                            raise UnsupportedDocument("Feishu response exceeds download limit")
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise UnsupportedDocument("Feishu response exceeds download limit")
                            chunks.append(chunk)
                        data = b"".join(chunks)
                        if binary and "json" not in response.headers.get("content-type", "").lower():
                            return data, response.headers
                        try:
                            payload = json.loads(data)
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise KnowledgeError("Feishu returned invalid JSON") from exc
                        if not isinstance(payload, dict):
                            raise KnowledgeError("Feishu returned invalid response shape")
                        code = payload.get("code")
                        if not isinstance(code, int):
                            raise KnowledgeError("Feishu response is missing a status code")
                        if code in (99991400, 99991403):
                            if attempt + 1 == self.max_attempts:
                                raise KnowledgeError("Feishu rate limit; retry limit reached")
                        elif code:
                            # Do not put response msg (which can echo credentials/content) in logs.
                            if code in (99991663, 99991668, 99991672, 99991679, 1063002, 131006):
                                raise AccessDenied(f"Feishu access denied (code {code})")
                            if code in (1063005, 1064001, 131005):
                                raise NotFound(f"Feishu resource unavailable (code {code})")
                            raise KnowledgeError(f"Feishu API rejected request (code {code})")
                        else:
                            if binary:
                                raise UnsupportedDocument("download returned JSON rather than a document")
                            return payload
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if attempt + 1 == self.max_attempts:
                    raise KnowledgeError("Feishu transport failed after bounded retries") from exc
            await self.sleep(retry_after)
        raise KnowledgeError("Feishu request retry limit reached")

    async def _data(self, path, *, params=None):
        response = await self._request("GET", path, params=params)
        data = response.get("data")
        if not isinstance(data, dict):
            raise KnowledgeError("Feishu response is missing data")
        return data

    async def _pages(self, path, *, params=None, item_key="items"):
        parameters, seen = dict(params or {}), set()
        for _ in range(self.max_pages):
            data = await self._data(path, params=parameters)
            items = data.get(item_key)
            if not isinstance(items, list):
                raise IncompleteSnapshot("Feishu page missing item list")
            if not isinstance(data.get("has_more"), bool):
                raise IncompleteSnapshot("Feishu page missing completeness flag")
            yield items
            if not data["has_more"]:
                return
            token = data.get("page_token")
            if not isinstance(token, str) or not token or token in seen:
                raise IncompleteSnapshot("Feishu pagination did not advance")
            seen.add(token)
            parameters["page_token"] = token
        raise IncompleteSnapshot("Feishu pagination limit reached")

    async def _public(self, token, kind):
        self._check_token(token)
        data = await self._data(f"/open-apis/drive/v1/permissions/{token}/public", params={"type": kind})
        permission = data.get("permission_public")
        if not isinstance(permission, dict):
            return None
        sharing = permission.get("link_share_entity")
        if sharing in self.PUBLIC_LINKS:
            return True
        if sharing in {"anyone_readable", "anyone_editable"}:
            # A globally shareable link still requires external access to be enabled.
            return True if permission.get("external_access") is True else None
        if sharing == "closed":
            return False
        return None

    async def _require_public(self, token, kind):
        try:
            public = await self._public(token, kind)
        except KnowledgeError as exc:
            # The caller must not classify a failed ACL refresh as just a content
            # fetch failure and extend an earlier permission lease.
            raise AccessDenied("current company-wide access cannot be verified") from exc
        if public is not True:
            raise AccessDenied("source is no longer verified company-wide")

    async def _verify_source_revision(self, node):
        try:
            node_token = self._check_token(node.metadata.get("node_token"))
        except ValueError as exc:
            raise AccessDenied("source has no verified Wiki lineage") from exc
        try:
            data = await self._data("/open-apis/wiki/v2/spaces/get_node", params={"token": node_token})
        except NotFound as exc:
            raise AccessDenied("Wiki source is no longer available") from exc
        current = data.get("node")
        if not isinstance(current, dict):
            raise IncompleteSnapshot("Wiki revision check is missing the node")
        if (current.get("node_token") != node_token or
                current.get("obj_token") != node.metadata["obj_token"] or
                current.get("obj_type") != node.metadata["obj_type"]):
            raise IncompleteSnapshot("Wiki source target changed after enumeration")
        revision = current.get("obj_edit_time")
        if revision in (None, "") or not node.revision or str(revision) != node.revision:
            raise IncompleteSnapshot("Wiki source changed after enumeration; retry in next sync")

    async def scan(self):
        nodes, errors, pending, visited, documents = [], [], deque(), set(), set()
        try:
            data = await self._data("/open-apis/wiki/v2/spaces/get_node", params={"token": self.root_token})
            root = data.get("node")
            if not isinstance(root, dict):
                raise IncompleteSnapshot("Wiki root missing node")
            pending.append(root)
        except (KnowledgeError, ValueError) as exc:
            return SourceSnapshot((), False, (f"root: {type(exc).__name__}",))
        while pending:
            raw = pending.popleft()
            try:
                node_token = self._check_token(raw.get("node_token"))
                if node_token in visited:
                    continue
                if len(visited) >= self.max_nodes:
                    errors.append("Wiki node limit reached")
                    break
                visited.add(node_token)
                if node_token in self.excluded_nodes:
                    # Explicit node exclusions include their subtree; never fetch ACL/content.
                    continue
                token = self._check_token(raw.get("obj_token"))
                if not isinstance(raw.get("has_child"), bool):
                    raise IncompleteSnapshot("Wiki node missing child enumeration flag")
                kind = raw.get("obj_type", "")
                if not isinstance(kind, str) or not re.fullmatch(r"[a-z0-9_]+", kind):
                    raise ValueError("invalid underlying type")
                key = f"{kind}:{token}"
                if key not in documents and key not in self.excluded_objects:
                    public, permission_error = None, ""
                    try:
                        public = await self._public(token, kind)
                    except KnowledgeError as exc:
                        permission_error = type(exc).__name__
                    metadata = {"obj_token": token, "obj_type": kind, "node_token": node_token,
                                "space_id": raw.get("space_id", ""), "permission_check": "document_link",
                                "permission_error": permission_error,
                                "acl_verified_at": time.time() if public is True else None}
                    nodes.append(SourceNode(key, str(raw.get("title") or token), str(raw.get("obj_edit_time") or ""),
                                            FEISHU_NATIVE_TYPE if kind in {"docx", "sheet"} else "application/octet-stream",
                                            f"{self.wiki_origin}/wiki/{node_token}", public, metadata))
                    documents.add(key)
                if raw.get("has_child"):
                    space = self._check_token(raw.get("space_id"))
                    try:
                        async for children in self._pages(f"/open-apis/wiki/v2/spaces/{space}/nodes",
                                                         params={"parent_node_token": node_token, "page_size": 50}):
                            # Inherit only the space identifier, never the parent's ACL.
                            if any(not isinstance(child, dict) for child in children):
                                raise IncompleteSnapshot("Wiki child page contains invalid nodes")
                            if len(children) + len(pending) + len(visited) > self.max_nodes:
                                raise IncompleteSnapshot("Wiki queued node limit reached")
                            pending.extend(dict(child, space_id=child.get("space_id") or space) for child in children)
                    except (KnowledgeError, TypeError, ValueError) as exc:
                        errors.append(f"children {node_token}: {type(exc).__name__}")
            except (KnowledgeError, TypeError, ValueError) as exc:
                errors.append(f"node: {type(exc).__name__}")
        return SourceSnapshot(tuple(nodes), not errors, tuple(errors))

    async def fetch(self, node):
        if node.metadata.get("node_token") in self.excluded_nodes or node.external_id in self.excluded_objects:
            raise AccessDenied("source is excluded from knowledge retrieval")
        token = self._check_token(node.metadata.get("obj_token"))
        kind = node.metadata.get("obj_type")
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z0-9_]+", kind):
            raise AccessDenied("invalid source type")
        if node.external_id != f"{kind}:{token}" or node.public is not True:
            raise AccessDenied("source publication has not been verified")
        await self._require_public(token, kind)
        if kind not in {"docx", "sheet", "file"}:
            raise UnsupportedDocument(f"原生飞书 {kind} 暂不支持；可提供 docx/xlsx/pptx/pdf 原文件")
        await self._verify_source_revision(node)
        content = await self._fetch_content(node, token, kind)
        # The download endpoint does not accept a file version. Detect a source
        # edit around the read rather than publish those bytes under an old ID.
        # This also catches worksheet topology changes around its range reads.
        await self._verify_source_revision(node)
        await self._require_public(token, kind)
        return content

    async def _docx_revision(self, token):
        data = await self._data(f"/open-apis/docx/v1/documents/{token}")
        document = data.get("document")
        revision = document.get("revision_id") if isinstance(document, dict) else None
        if type(revision) is not int or revision < 0:
            raise IncompleteSnapshot("Docx revision is missing or invalid")
        return revision

    async def _fetch_content(self, node, token, kind):
        if kind == "docx":
            revision = await self._docx_revision(token)
            blocks, collected_size = [], 0
            # Explicit historical revisions can require privileges beyond
            # ordinary document reads. Read latest (-1), then verify that its
            # revision stayed unchanged across the complete paginated read.
            async for page in self._pages(f"/open-apis/docx/v1/documents/{token}/blocks",
                                         params={"page_size": 500, "document_revision_id": -1}):
                collected_size += len(json.dumps(page, ensure_ascii=False).encode("utf-8"))
                if collected_size > self.max_download_bytes:
                    raise UnsupportedDocument("Docx cumulative block size limit exceeded")
                blocks.extend(page)
                if len(blocks) > 50000:
                    raise UnsupportedDocument("Docx block limit exceeded")
            if await self._docx_revision(token) != revision:
                raise IncompleteSnapshot("Docx changed during block reads; retry in next sync")
            return self._native_content({"kind": "docx", "revision": revision, "blocks": blocks}, node.title)
        if kind == "sheet":
            data = await self._data(f"/open-apis/sheets/v3/spreadsheets/{token}/sheets/query")
            sheets = data.get("sheets")
            if not isinstance(sheets, list):
                raise KnowledgeError("spreadsheet missing sheet list")
            output, total_cells, collected_size, sheet_revision = [], 0, 0, None
            for sheet in sheets:
                sheet_id = self._check_token(sheet.get("sheet_id"))
                grid = sheet.get("grid_properties", {})
                rows, columns = grid.get("row_count"), grid.get("column_count")
                if not isinstance(rows, int) or not isinstance(columns, int) or rows < 0 or columns < 0:
                    raise UnsupportedDocument("spreadsheet grid size is unavailable")
                total_cells += rows * columns
                if total_cells > self.max_sheet_cells:
                    raise UnsupportedDocument("spreadsheet exceeds configured cell limit")
                record = {"id": sheet_id, "title": str(sheet.get("title") or sheet_id), "headers": [], "rows": []}
                for start in range(1, rows + 1, 200):
                    if not columns:
                        break
                    cell_range = f"{sheet_id}!A{start}:{self._column(columns)}{min(rows, start + 199)}"
                    values_data = await self._data(f"/open-apis/sheets/v2/spreadsheets/{token}/values/{quote(cell_range, safe='')}",
                                                   params={"valueRenderOption": "ToString", "dateTimeRenderOption": "FormattedString"})
                    current_revision = values_data.get("revision")
                    if not isinstance(current_revision, int):
                        raise IncompleteSnapshot("spreadsheet response revision is missing")
                    if sheet_revision is not None and current_revision != sheet_revision:
                        raise IncompleteSnapshot("spreadsheet changed during range reads; retry in next sync")
                    sheet_revision = current_revision
                    values = values_data.get("valueRange", {}).get("values")
                    if not isinstance(values, list):
                        raise KnowledgeError("spreadsheet values are missing")
                    if len(values) > min(200, rows - start + 1) or any(not isinstance(row, list) or len(row) > columns for row in values):
                        raise KnowledgeError("spreadsheet values exceed requested grid")
                    collected_size += len(json.dumps(values, ensure_ascii=False).encode("utf-8"))
                    if collected_size > self.max_download_bytes:
                        raise UnsupportedDocument("spreadsheet cumulative content size limit exceeded")
                    if start == 1 and values:
                        record["headers"] = values[0]
                    record["rows"].extend({"row": start + index, "values": row} for index, row in enumerate(values))
                output.append(record)
            return self._native_content({"kind": "sheet", "revision": sheet_revision, "sheets": output}, node.title)
        if kind == "file":
            data, headers = await self._request("GET", f"/open-apis/drive/v1/files/{token}/download", binary=True)
            disposition = Message()
            disposition["content-disposition"] = headers.get("content-disposition", "")
            filename = disposition.get_filename() or node.title
            filename = PurePath(str(filename).replace("\\", "/")).name
            media = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            return SourceContent(data, filename, media)
        raise UnsupportedDocument(f"原生飞书 {kind} 暂不支持")

    def _native_content(self, value, title):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > self.max_download_bytes:
            raise UnsupportedDocument("native document exceeds configured size limit")
        return SourceContent(data, title + ".feishu.json", FEISHU_NATIVE_TYPE)

    @staticmethod
    def _column(number):
        result = ""
        while number:
            number, digit = divmod(number - 1, 26)
            result = chr(65 + digit) + result
        return result
