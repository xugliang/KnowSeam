import tempfile
import unittest
from pathlib import Path

import httpx

from knowseam.connectors import FeishuWikiConnector, FilesystemConnector
from knowseam.domain import AccessDenied, IncompleteSnapshot, KnowledgeError, SourceNode, UnsupportedDocument
from knowseam.parsers import StructuredDocumentParser


def success(data):
    return httpx.Response(200, json={"code": 0, "data": data})


def wiki_node(token="root", kind="docx", *, children=False, title="产品知识"):
    return {"node_token": token, "obj_token": "obj_" + token, "obj_type": kind,
            "space_id": "123", "title": title, "has_child": children, "obj_edit_time": "1700000010"}


class FeishuFixture:
    def __init__(self, handler):
        self.handler, self.requests = handler, []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "test-only", "expire": 7200})
        assert request.method == "GET", "all resource APIs must remain read-only"
        assert request.url.host == "open.feishu.cn"
        assert request.headers["Authorization"] == "Bearer test-only"
        return self.handler(request)


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def make_connector(self, handler, **kwargs):
        fixture = FeishuFixture(handler)
        async def no_sleep(_):
            pass
        connector = FeishuWikiConnector(app_id="fixture-app", app_secret="fixture-secret", root_token="root",
                                        transport=httpx.MockTransport(fixture), sleep=no_sleep, **kwargs)
        self.addAsyncCleanup(connector.close)
        return connector, fixture

    async def test_wiki_pagination_per_file_acl_and_unknown_fail_closed(self):
        permissions = {"obj_root": "tenant_readable", "obj_public": "tenant_editable", "obj_private": "closed"}
        def handler(request):
            path = request.url.path
            if path.endswith("get_node"):
                return success({"node": wiki_node(children=True)})
            if path.endswith("/nodes"):
                if request.url.params.get("page_token") == "next":
                    return success({"items": [wiki_node("private"), wiki_node("unknown")], "has_more": False})
                return success({"items": [wiki_node("public")], "has_more": True, "page_token": "next"})
            token = path.split("/")[-2]
            permission = {"link_share_entity": permissions[token]} if token in permissions else {}
            return success({"permission_public": permission})
        connector, fixture = await self.make_connector(handler)
        snapshot = await connector.scan()
        self.assertTrue(snapshot.complete)
        self.assertEqual([node.public for node in snapshot.nodes], [True, True, False, None])
        self.assertIsInstance(snapshot.nodes[0].metadata["acl_verified_at"], float)
        self.assertIsNone(snapshot.nodes[-1].metadata["acl_verified_at"])
        self.assertEqual(len([r for r in fixture.requests if "/permissions/" in r.url.path]), 4)
        with self.assertRaises(AccessDenied):
            await connector.fetch(snapshot.nodes[-1])

    async def test_partial_listing_does_not_claim_complete_snapshot(self):
        def handler(request):
            if request.url.path.endswith("get_node"):
                return success({"node": wiki_node(children=True)})
            if "/permissions/" in request.url.path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            if not request.url.params.get("page_token"):
                return success({"items": [wiki_node("child")], "has_more": True, "page_token": "next"})
            return httpx.Response(503)
        connector, fixture = await self.make_connector(handler)
        snapshot = await connector.scan()
        self.assertFalse(snapshot.complete)
        self.assertEqual(len(snapshot.nodes), 2)
        failed = [r for r in fixture.requests if r.url.params.get("page_token") == "next"]
        self.assertEqual(len(failed), 3)

    async def test_repeated_page_token_cannot_infinite_loop_or_delete(self):
        def handler(request):
            if request.url.path.endswith("get_node"):
                return success({"node": wiki_node(children=True)})
            if "/permissions/" in request.url.path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            return success({"items": [], "has_more": True, "page_token": "same"})
        connector, fixture = await self.make_connector(handler)
        self.assertFalse((await connector.scan()).complete)
        self.assertEqual(len([r for r in fixture.requests if r.url.path.endswith("/nodes")]), 2)

    async def test_missing_pagination_flag_cannot_claim_a_complete_snapshot(self):
        def handler(request):
            if request.url.path.endswith("get_node"):
                return success({"node": wiki_node(children=True)})
            if "/permissions/" in request.url.path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            return success({"items": []})
        connector, _ = await self.make_connector(handler)
        snapshot = await connector.scan()
        self.assertFalse(snapshot.complete)
        self.assertTrue(snapshot.errors)

    async def test_non_docx_root_still_recurses_and_bad_child_page_is_incomplete(self):
        def handler(request):
            if request.url.path.endswith("get_node"):
                return success({"node": wiki_node(kind="mindnote", children=True)})
            if "/permissions/" in request.url.path:
                self.assertIn(request.url.params["type"], {"mindnote", "sheet"})
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            if request.url.params["parent_node_token"] == "root":
                child = wiki_node("child", kind="sheet", children=True)
                child.pop("space_id")
                return success({"items": [child], "has_more": False})
            self.assertEqual(request.url.params["parent_node_token"], "child")
            return success({"items": ["malformed"], "has_more": False})
        connector, _ = await self.make_connector(handler)
        snapshot = await connector.scan()
        self.assertFalse(snapshot.complete)
        self.assertEqual([node.external_id for node in snapshot.nodes], ["mindnote:obj_root", "sheet:obj_child"])
        self.assertEqual(snapshot.nodes[1].metadata["space_id"], "123")

    async def test_rate_limit_third_attempt_succeeds_and_timeout_stops_at_three(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            if count < 3:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return success({"node": wiki_node()})
        connector, _ = await self.make_connector(handler)
        await connector._data("/open-apis/wiki/v2/spaces/get_node")
        self.assertEqual(count, 3)
        count = 0
        def timeout(request):
            nonlocal count
            count += 1
            raise httpx.ReadTimeout("fixture timeout", request=request)
        second, _ = await self.make_connector(timeout)
        with self.assertRaises(KnowledgeError):
            await second._data("/open-apis/wiki/v2/spaces/get_node")
        self.assertEqual(count, 3)

    async def test_docx_native_blocks_real_parser_latest_with_revision_verification(self):
        block = {"block_id": "b1", "block_type": 2, "text": {"elements": [{"text_run": {"content": "支持视频生成"}}]}}
        def handler(request):
            path = request.url.path
            if path.endswith("get_node"):
                return success({"node": wiki_node()})
            if "/permissions/" in path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            if path.endswith("/blocks"):
                self.assertEqual(request.url.params["document_revision_id"], "-1")
                return success({"items": [block], "has_more": False})
            return success({"document": {"revision_id": 19}})
        connector, fixture = await self.make_connector(handler)
        snapshot = await connector.scan()
        parsed = StructuredDocumentParser().parse(await connector.fetch(snapshot.nodes[0]))
        self.assertEqual(parsed.sections[0].text, "支持视频生成")
        self.assertEqual(parsed.sections[0].locator["block_id"], "b1")
        metadata_reads = [r for r in fixture.requests if r.url.path.endswith("/documents/obj_root")]
        self.assertEqual(len(metadata_reads), 2)

    async def test_docx_edit_between_latest_pages_rejects_unstable_snapshot(self):
        metadata_reads, block_pages = 0, 0
        def handler(request):
            nonlocal metadata_reads, block_pages
            path = request.url.path
            if path.endswith("get_node"):
                # Wiki edit timestamps need not advance within this short read.
                return success({"node": wiki_node()})
            if "/permissions/" in path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            if path.endswith("/blocks"):
                self.assertEqual(request.url.params["document_revision_id"], "-1")
                block_pages += 1
                if block_pages == 1:
                    return success({"items": [], "has_more": True, "page_token": "next"})
                self.assertEqual(request.url.params["page_token"], "next")
                return success({"items": [], "has_more": False})
            metadata_reads += 1
            return success({"document": {"revision_id": 19 if metadata_reads == 1 else 20}})
        connector, _ = await self.make_connector(handler)
        node = (await connector.scan()).nodes[0]
        def must_not_package(*args):
            self.fail("inconsistent Docx snapshot must not be returned for parsing/publication")
        connector._native_content = must_not_package
        with self.assertRaisesRegex(IncompleteSnapshot, "changed during block reads"):
            await connector.fetch(node)
        self.assertEqual((metadata_reads, block_pages), (2, 2))

    async def test_docx_missing_or_invalid_revision_before_and_after_read_is_rejected(self):
        for broken in [None, {}, {"revision_id": None}, {"revision_id": True},
                       {"revision_id": -1}, {"revision_id": "19"}, {"revision_id": 1.5}]:
            for stage in ["before", "after"]:
                with self.subTest(document=broken, stage=stage):
                    metadata_reads, block_pages = 0, 0
                    def handler(request):
                        nonlocal metadata_reads, block_pages
                        path = request.url.path
                        if path.endswith("get_node"):
                            return success({"node": wiki_node()})
                        if "/permissions/" in path:
                            return success({"permission_public": {"link_share_entity": "tenant_readable"}})
                        if path.endswith("/blocks"):
                            self.assertEqual(request.url.params["document_revision_id"], "-1")
                            block_pages += 1
                            return success({"items": [], "has_more": False})
                        metadata_reads += 1
                        document = broken if stage == "before" or metadata_reads == 2 else {"revision_id": 19}
                        return success({"document": document})
                    connector, _ = await self.make_connector(handler)
                    node = (await connector.scan()).nodes[0]
                    with self.assertRaisesRegex(IncompleteSnapshot, "revision is missing or invalid"):
                        await connector.fetch(node)
                    self.assertEqual(block_pages, 0 if stage == "before" else 1)

    async def test_docx_latest_works_without_historical_revision_permission(self):
        def handler(request):
            path = request.url.path
            if path.endswith("get_node"):
                return success({"node": wiki_node()})
            if "/permissions/" in path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            if path.endswith("/blocks"):
                if request.url.params.get("document_revision_id") != "-1":
                    return httpx.Response(403, json={"code": 1770032, "msg": "forbidden"})
                return success({"items": [{"block_id": "b1", "block_type": 2,
                    "text": {"elements": [{"text_run": {"content": "最新版本可读"}}]}}], "has_more": False})
            return success({"document": {"revision_id": 19}})
        connector, fixture = await self.make_connector(handler)
        node = (await connector.scan()).nodes[0]
        parsed = StructuredDocumentParser().parse(await connector.fetch(node))
        self.assertEqual(parsed.sections[0].text, "最新版本可读")
        self.assertFalse(any("raw_content" in request.url.path for request in fixture.requests))

    async def test_native_sheet_fetch_all_worksheets_and_rows(self):
        def handler(request):
            path = request.url.path
            if path.endswith("get_node"):
                return success({"node": wiki_node(kind="sheet")})
            if "/permissions/" in path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            if path.endswith("/sheets/query"):
                return success({"sheets": [{"sheet_id": "tab1", "title": "Kino", "grid_properties": {"row_count": 2, "column_count": 2}},
                                           {"sheet_id": "tab2", "title": "示例产品", "grid_properties": {"row_count": 2, "column_count": 2}}]})
            return success({"revision": 8, "valueRange": {"values": [["产品", "功能"], ["测试产品", "测试功能"]]}})
        connector, _ = await self.make_connector(handler)
        nodes = (await connector.scan()).nodes
        parsed = StructuredDocumentParser().parse(await connector.fetch(nodes[0]))
        self.assertEqual({section.locator["sheet"] for section in parsed.sections}, {"Kino", "示例产品"})
        self.assertEqual(len(parsed.sections), 4)

    async def test_sheet_change_during_multi_range_fetch_is_not_published(self):
        reads = 0
        def handler(request):
            nonlocal reads
            if request.url.path.endswith("get_node"):
                return success({"node": wiki_node(kind="sheet")})
            if "/permissions/" in request.url.path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            if request.url.path.endswith("/sheets/query"):
                return success({"sheets": [{"sheet_id": "tab1", "title": "Kino",
                    "grid_properties": {"row_count": 300, "column_count": 2}}]})
            reads += 1
            return success({"revision": reads, "valueRange": {"values": [["产品", "功能"]]}})
        connector, _ = await self.make_connector(handler)
        node = SourceNode("sheet:obj_root", "表格", "1700000010", "", "", True,
                          {"obj_type": "sheet", "obj_token": "obj_root", "node_token": "root"})
        with self.assertRaisesRegex(KnowledgeError, "changed during"):
            await connector.fetch(node)

    async def test_revoked_permission_denies_fetch_even_after_public_scan(self):
        calls = 0
        def handler(request):
            nonlocal calls
            if request.url.path.endswith("get_node"):
                return success({"node": wiki_node()})
            calls += 1
            return success({"permission_public": {"link_share_entity": "tenant_readable" if calls == 1 else "closed"}})
        connector, fixture = await self.make_connector(handler)
        node = (await connector.scan()).nodes[0]
        with self.assertRaises(AccessDenied):
            await connector.fetch(node)
        self.assertFalse(any("/docx/" in r.url.path for r in fixture.requests))

    async def test_download_size_redirect_and_native_slides_rejected(self):
        mode = "large"
        def handler(request):
            if request.url.path.endswith("get_node"):
                return success({"node": wiki_node(kind="file")})
            if "/permissions/" in request.url.path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            if mode == "large":
                return httpx.Response(200, content=b"a" * 500, headers={"Content-Type": "application/octet-stream"})
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/credentials"})
        connector, fixture = await self.make_connector(handler, max_download_bytes=300)
        node = SourceNode("file:obj_root", "a.txt", "1700000010", "", "", True,
                          {"obj_type": "file", "obj_token": "obj_root", "node_token": "root"})
        with self.assertRaises(UnsupportedDocument):
            await connector.fetch(node)
        mode = "redirect"
        with self.assertRaises(AccessDenied):
            await connector.fetch(node)
        self.assertTrue(all(request.url.host == "open.feishu.cn" for request in fixture.requests))
        node = SourceNode("slides:obj_root", "原生幻灯片", "1", "", "", True,
                          {"obj_type": "slides", "obj_token": "obj_root"})
        with self.assertRaises(UnsupportedDocument):
            await connector.fetch(node)

    async def test_attachment_download_filename_and_no_resource_writes(self):
        def handler(request):
            if request.url.path.endswith("get_node"):
                return success({"node": wiki_node(kind="file")})
            if "/permissions/" in request.url.path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            return httpx.Response(200, content="公司产品说明".encode(),
                                  headers={"Content-Type": "text/plain", "Content-Disposition": "attachment; filename=manual.txt"})
        connector, fixture = await self.make_connector(handler, wiki_origin="https://example.feishu.cn")
        node = SourceNode("file:obj_root", "无扩展名", "1700000010", "", "", True,
                          {"obj_type": "file", "obj_token": "obj_root", "node_token": "root"})
        content = await connector.fetch(node)
        self.assertEqual(content.filename, "manual.txt")
        self.assertEqual(StructuredDocumentParser().parse(content).sections[0].text, "公司产品说明")
        with self.assertRaises(AccessDenied):
            await connector._request("POST", "/open-apis/drive/v1/import_tasks")
        self.assertEqual(len([r for r in fixture.requests if r.method != "GET"]), 1)
        self.assertEqual({r.url.host for r in fixture.requests}, {"open.feishu.cn"})

    async def test_attachment_change_during_download_rejected_then_next_scan_recovers(self):
        downloaded = False
        def handler(request):
            nonlocal downloaded
            if request.url.path.endswith("get_node"):
                node = wiki_node(kind="file", title="manual.txt")
                if downloaded:
                    node["obj_edit_time"] = "1700000020"
                return success({"node": node})
            if "/permissions/" in request.url.path:
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            downloaded = True
            return httpx.Response(200, content=b"new content", headers={"Content-Type": "text/plain"})
        connector, _ = await self.make_connector(handler)
        old = (await connector.scan()).nodes[0]
        with self.assertRaisesRegex(KnowledgeError, "changed after enumeration"):
            await connector.fetch(old)
        current = (await connector.scan()).nodes[0]
        self.assertNotEqual(current.revision, old.revision)
        self.assertEqual((await connector.fetch(current)).data, b"new content")

    async def test_permission_unknown_after_content_read_is_access_denied(self):
        acl_reads = 0
        def handler(request):
            nonlocal acl_reads
            if request.url.path.endswith("get_node"):
                return success({"node": wiki_node(kind="file", title="manual.txt")})
            if "/permissions/" in request.url.path:
                acl_reads += 1
                if acl_reads >= 3:
                    return httpx.Response(503)
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            return httpx.Response(200, content=b"content", headers={"Content-Type": "text/plain"})
        connector, _ = await self.make_connector(handler)
        node = (await connector.scan()).nodes[0]
        with self.assertRaisesRegex(AccessDenied, "cannot be verified"):
            await connector.fetch(node)

    async def test_acl_success_is_recorded_independently_of_content_failure(self):
        def handler(request):
            if request.url.path.endswith("get_node"):
                return success({"node": wiki_node()})
            if "/permissions/" in request.url.path:
                self.assertEqual(request.url.params["type"], "docx")
                return success({"permission_public": {"link_share_entity": "tenant_readable"}})
            return httpx.Response(503)
        connector, _ = await self.make_connector(handler)
        node = (await connector.scan()).nodes[0]
        self.assertTrue(node.public)
        self.assertGreater(node.metadata["acl_verified_at"], 0)
        with self.assertRaises(KnowledgeError) as failure:
            await connector.fetch(node)
        self.assertNotIsInstance(failure.exception, AccessDenied)
        self.assertTrue(node.public)

    async def test_invalid_origins_and_identifiers_rejected(self):
        for origin in ["http://open.feishu.cn", "https://feishu.cn.evil.example", "https://localhost", "https://open.feishu.cn:8443"]:
            with self.assertRaises(ValueError):
                FeishuWikiConnector(app_id="a", app_secret="s", root_token="root", wiki_origin=origin)
        with self.assertRaises(ValueError):
            FeishuWikiConnector(app_id="a", app_secret="s", root_token="../secret")

    async def test_filesystem_explicit_publication_and_content_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "public.md").write_text("产品资料", encoding="utf-8")
            (path / "private.txt").write_text("不可收录", encoding="utf-8")
            connector = FilesystemConnector(path, public_files=("public.md",))
            first = await connector.scan()
            self.assertTrue(first.complete)
            self.assertEqual(len(first.nodes), 1)
            (path / "public.md").write_text("更新后的产品资料", encoding="utf-8")
            self.assertNotEqual(first.nodes[0].revision, (await connector.scan()).nodes[0].revision)
            self.assertIn("更新", (await connector.fetch(first.nodes[0])).data.decode())
            private = SourceNode("private.txt", "", "", "", "", True)
            with self.assertRaises(AccessDenied):
                await connector.fetch(private)
            escaped = FilesystemConnector(path, public_files=("../outside.txt",))
            self.assertFalse((await escaped.scan()).complete)


if __name__ == "__main__":
    unittest.main()
