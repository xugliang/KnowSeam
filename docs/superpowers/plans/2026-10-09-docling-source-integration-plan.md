# KnowSeam Docling 源码整合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 KnowSeam 单仓库内维护完整 Docling 源码，交付可离线、可更新、可定制的通用文档解析能力，并接入现有增量知识库。

**Architecture:** Git subtree 保存完整上游源码，KnowSeam 适配现有 DocumentParser ABC。轻量同步 worker 通过 Unix socket 请求同仓库受限解析容器，后者没有网络、来源凭据或数据库访问；输出进入既有切片、BM25/向量检索、重排序和 API/MCP。

**Tech Stack:** Python 3.12、Pydantic、httpx、Starlette/Uvicorn、Docling 2.135.0、本地 RapidOCR ONNX、PostgreSQL/pgvector、jieba、Git subtree、Docker Compose。

**Spec:** `docs/superpowers/specs/2026-10-09-docling-source-integration-design.md`，已获用户书面确认。

## Global Constraints

- KnowSeam 是独立、通用的企业知识库能力；产品 A 只是消费者之一。不修改 SuiteHarness 或产品 A/B 源码，不改变对外检索协议。
- 完整源码位于 `third_party/docling/`，使用 `git subtree --squash`，保留上游 LICENSE 与 subtree 合并元数据；不安装 PyPI 同名实现替代本地源码。
- 固定基线标签 `v2.135.0`，提交 `2b3a00c5bfcd8b218d32e7e9c33d509aeaa0cb56`；实施时用 Git 再验证标签解析出的 SHA，不匹配则停止导入。
- parsing.docling.enabled 默认 false；旧配置、API/MCP、原生同步路径无需 Docling 或模型即可运行。首批格式 PDF/扫描 PDF、PNG/JPEG/TIFF、DOCX/XLSX/PPTX；飞书原生 JSON、Markdown、文本保持原生路径。
- timeout_seconds=120、max_pages=200、max_sections=50000；OCR 和 PDF 表格结构开关默认 true；socket_path=/run/knowseam-parser/parser.sock。
- 现有 30 MiB 文件、150 MiB Office 展开、宏/加密拒绝等保护不能被新路由绕过。调用者不能提供任意 URL、本地路径、模型路径或环境变量给执行器。
- 解析容器非 root、只读根文件系统、network_mode:none，不挂载来源配置、数据库、Docker socket或其他项目；仅模型只读目录、Unix socket卷和临时 tmpfs。
- 当前代码及未提交的企业词典、权限、来源排除等改动必须保留，不混入本次提交，不把企业资料、配置或模型权重推送 GitHub。
- 测试使用合成/公开资料及隔离数据库，禁止调用真实生产 DSN、在线生成模型或员工发送接口。不重启 A/B，不修改服务器全局配置。
- 回滚针对当前有效源 revision 重新解析；不能恢复过时历史内容、旧授权或已删除文档。

## Review Focus

1. 中文文件名、伪造扩展名、异常媒体类型：不乱码、不误路由，不放宽文件验证；Task 3 的 `test_unicode_name_and_mismatched_format`。
2. Unix socket 与解析器重启：不删除其他进程的活动 socket，worker 得到明确失败且旧索引保留；Task 2 的 `test_active_or_foreign_socket_is_not_unlinked` 与 Task 4 的不可用测试。
3. 行合并、多级表头、超长单元格：不丢字段关系或来源定位，不生成额外事实；Task 3 的表格测试及 Task 5 的真实格式测试。
4. 文档解析过程中撤权/删除，以及回滚时源已更新：不能重新授权或检索历史答案；Task 4/5 的撤权、当前 revision 回滚测试。
5. 上游包结构变化与模型缓存不完整：构建失败或明确报模型缺失，不能从 PyPI/公网静默兜底；Task 1 的本地源码测试及 Task 5 的离线缺模型测试。

## 工作区、文件职责及交付记录

实施从服务器 `/srv/knowseam` 当前工作树开始。先记录 HEAD、完整 Git 状态和相关源文件摘要，在经核实的项目隔离目录建立开发 worktree；将当前已修改的非私密源码与测试作为隔离工作树基线保留。只复制需要的源码，不复制 config.local.json、config.container.json、secrets、data、models、backups。

已有修改不提交、不重置；每个任务只记录自己的差异。新文件可以单独提交；对 cli.py、config.py 等已有改动无法可靠分离时保留任务差异补丁与证据，不能为满足提交步骤而混入已有修改。最终整合回当前工作树前比较初始摘要和三方差异；发生外部并发变化则停下处理重叠，不能覆盖。

新增配置、协议、客户端、执行器、Docling 适配、结构规范化、指纹各自单独文件，放在 `src/knowseam/document_parsing/`；保留原有 `parsers.py`。部署文件归 deployment，维护脚本归 scripts，测试归 tests/document_parsing。只在确有需要时修改原生安全检查、切片和同步流程，不做无关重构。

开发记录放在项目隔离区，逐任务记录失败/通过测试、提交或差异补丁、待修问题。下面命令均在隔离工作树与项目专用环境执行，不使用生产配置。

---

### Task 1: 完整源码引入、本地构建与可追踪升级

**Files:** Create `third_party/docling/`、`third_party/docling.upstream.json`、`scripts/build-vendored-docling.sh`、`scripts/docling-upstream-update.sh`、`scripts/lock-parser-dependencies.sh`、`deployment/requirements-parser.in`、`deployment/requirements-parser.lock`、`tests/document_parsing/test_vendor.py`；Modify `NOTICE.md`。

**Interfaces:**
- Produces 构建元数据 JSON：upstream_url、upstream_ref、upstream_sha、runtime_source_sha256、distribution_name、distribution_version、dependency_fingerprint。实际源码包固定 docling-slim=2.135.0，Python 模块名 docling。
- Produces wheelhouse 内的本地 docling-slim wheel；外部依赖精确版本和哈希，必须从本地 wheelhouse 安装真实实现。
- 锁定脚本仅在人工执行时解析/下载依赖到项目目录，不部署服务；上游更新脚本仅在干净 worktree 的升级分支合并，不自动推送或重启。

- [ ] **Step 1: 写失败测试**：`test_fixed_upstream_identity` 校验 ref/SHA/name/version；`test_installed_module_matches_vendored_runtime_source` 校验 wheel 内运行模块文件摘要与 subtree 对应；`test_update_refuses_dirty_worktree` 用临时 Git 仓库证明已有改动时拒绝合并；`test_packaging_layout_mismatch_fails_closed` 证明包装包不能替代源码包。

```python
def test_fixed_upstream_identity():
    identity = json.loads(Path("third_party/docling.upstream.json").read_text())
    assert identity["upstream_ref"] == "v2.135.0"
    assert identity["upstream_sha"] == "2b3a00c5bfcd8b218d32e7e9c33d509aeaa0cb56"
    assert identity["distribution_name"] == "docling-slim"
```
- [ ] **Step 2: 执行红灯测试**：`python -m pytest tests/document_parsing/test_vendor.py -q`，期望新增构建工具/元数据尚不存在而失败，记录原因，不能因为环境配置错误当作功能红灯。
- [ ] **Step 3: 实施源码和构建**：验证远程标签与 SHA 后 subtree add；使用固定 SHA 读取到的实际打包布局。选择 extras `convert-core,format-pdf,format-office,models-local,feat-ocr-rapidocr-onnx,cli`，不安装 all/VLM/audio。按Linux/Python3.12/CPU目标锁定整个解析镜像依赖，明确CPU版torch来源，不能默认下载CUDA依赖；不直接沿用上游开发uv.lock。锁文件不得残留机器专属绝对源码路径；本地源码wheel独立记录hash。构建本地wheel并用no-index/no-deps安装实现，依赖来自已核验wheelhouse。源码和适配器运行摘要作为构建产物，不依赖镜像中存在.git。
- [ ] **Step 4: 执行绿灯及供应来源验证**：运行上述测试；在项目专用环境构建 wheel，验证 `importlib.metadata` 与实际 import docling 的内容摘要，修改临时副本运行源码后期望摘要改变。检查 MIT 声明与依赖/模型许可清单，不复制企业资料。
- [ ] **Step 5: 保存任务提交/差异**：仅新增第三方源码、构建维护工具及许可变更；保留 subtree 提交序列，不再二次 squash 此导入历史。

### Task 2: 默认关闭配置与受限解析执行协议

**Files:** Create `src/knowseam/document_parsing/__init__.py`、`config.py`、`protocol.py`、`client.py`、`executor.py`、`process_runner.py`、`tests/document_parsing/test_config.py`、`test_protocol.py`、`test_process_runner.py`；Modify `src/knowseam/config.py`、`tests/test_config.py`。

**Interfaces:**
- `ParsingConfig` / `DoclingConfig` 严格 Pydantic 模型，默认和约束来自 Global Constraints。model_path 指解析容器路径，不要求 worker 挂载模型；执行器启动时核验模型可读性和身份。
- `ParseMetadata(filename: str, media_type: str, options: DoclingOptions)`；不允许额外字段，filename 是单一文件名，不是路径/URL。`DoclingOptions` 只含 ocr_enabled、table_structure_enabled、timeout_seconds、max_pages、max_sections；只能降低执行器上限。
- `ParserIdentity` 包含 protocol_version=1、upstream_sha、runtime_source_sha256、dependency_fingerprint、model_fingerprint、normalizer_version；不返回凭据或源文档内容。`ParserClient(socket_path: Path, *, timeout_seconds: int, max_bytes: int)` 提供 `status() -> ParserIdentity`、`parse(content: SourceContent, options: DoclingOptions) -> ParsedDocument`、`close() -> None`。
- Unix HTTP：`GET /v1/status` 返回协议版本和非敏感 ParserIdentity；`POST /v1/parse` body 为原始字节，ASCII header `X-KnowSeam-Parse-Metadata` 是 UTF-8 JSON 的 Base64 编码，上限 8192 字节。流式累计 body 不超过 max_bytes；响应为有大小上限的 sections/warnings/parser_version JSON。协议版本固定 1，不使用 pickle。
- `ExecutorSettings` 包含 socket_path、model_path、identity 及服务上限 max_bytes=30*1024*1024、timeout_seconds=120、max_pages=200、max_sections=50000、max_text_chars=150*1024*1024、max_response_bytes=150*1024*1024、max_metadata_bytes=8192；请求只能降低数值上限。`create_executor_app(settings: ExecutorSettings, *, runner: ParseRunner) -> Starlette`；`ParseRunner.run(content: SourceContent, options: DoclingOptions) -> ParsedDocument` 在可回收 spawn 子进程执行；测试注入安全替身。
- 执行器入口 `python -m knowseam.document_parsing.executor --socket PATH --model-path PATH`，不读取 KnowSeam DSN/clients/sources 配置，不监听 TCP。

- [ ] **Step 1: 写失败测试**：旧配置默认 enabled=false、limits=120/200/50000；字符串数字/bool作为整数、未知字段、遍历路径/URL/重复配置均拒绝；中文文件名协议可往返；请求超限/畸形Base64/伪造sections/非有限locator值不进入runner。通过替身验证不存在 DSN/飞书凭据读取。已有活动或非本UID socket不得自动删除。

```python
def test_default_limits_and_disabled_engine():
    config = ParsingConfig()
    assert config.docling.enabled is False
    assert (config.docling.timeout_seconds, config.docling.max_pages,
            config.docling.max_sections) == (120, 200, 50000)
    with pytest.raises(ValidationError):
        DoclingConfig(timeout_seconds=True)
```
- [ ] **Step 2: 红灯**：`python -m pytest tests/document_parsing/test_config.py tests/document_parsing/test_protocol.py tests/document_parsing/test_process_runner.py -q`。
- [ ] **Step 3: 实施配置、协议和执行器**：配置解析不导入 Docling；客户端只使用 httpx Unix transport，不允许 redirects。执行器只读取上述入口参数与不含凭据的模型清单；runner 使用最小环境、限制线程和并发，超时 terminate/kill/join 仅回收自己创建的子进程。返回清晰分类错误，不返回原始第三方异常、文件正文或环境内容。目录和socket检查不采用宽泛删除。
- [ ] **Step 4: 绿灯及旧配置兼容**：运行新测试加 `python -m pytest tests/test_config.py tests/test_cli.py -q`。注入挂起runner验证超时及后续请求可用；executor不需要数据库配置即可启动并返回status。
- [ ] **Step 5: 保存任务提交/差异**：保留原有权限、企业词典等改动；配置字段仅增加 parsing，不改原 clients/collections/sources 语义。

### Task 3: Docling 适配、结构规范化、路由与表格上下文

**Files:** Create `docling_adapter.py`、`normalizer.py`、`router.py`、`factory.py`、`fingerprint.py`、`extensions/tables.py`、`extensions/cleanup.py`（均在 document_parsing）；Create `tests/document_parsing/test_adapter.py`、`test_router.py`、`test_normalizer.py`、`test_fingerprint.py`；Modify `src/knowseam/parsers.py`、`src/knowseam/chunking.py`、`tests/test_chunking.py`。

**Interfaces:**
- `normalize_document(document: object, *, parser_version: str, max_sections: int, max_text_chars: int) -> ParsedDocument`；测试用具有真实结构的 fixture，真实类型仅在增强执行环境导入。
- `DoclingDocumentParser(model_path: Path, identity: ParserIdentity)` 实现 DocumentParser，只在执行器使用；明确 `RapidOcrOptions(backend='onnxruntime', lang=['ch'], model_size='small')`，禁用远程服务/自动下载，按选定模型清单定位 artifacts。
- `DoclingAdapter(client: ParserClient, options: DoclingOptions)` 实现 DocumentParser，用于worker；`version: str` 来自身份与选项，不依赖单次输出。
- `RoutingDocumentParser(native: DocumentParser, enhanced: DocumentParser, routes: Mapping[str,str])`；原生飞书媒体类型优先，不被扩展名覆盖，明确路由失败不静默换引擎。
- `build_parser(settings: ParsingConfig, *, max_bytes: int, client: ParserClient | None = None) -> DocumentParser`；关闭时直接返回 StructuredDocumentParser，不触发socket、模型或Docling导入。
- `parser_fingerprint(identity: ParserIdentity, options: DoclingOptions, *, normalizer_version: str) -> str`；全局路由 version 是所用引擎/映射/输出相关参数摘要，ParsedDocument.parser_version 是具体引擎身份，二者同源而不要求字符串相等。

- [ ] **Step 1: 写失败测试**：关闭时 monkeypatch Docling/socket 操作必失败但原生仍能解析；原生飞书不能被路由至Docling；中文文件名/格式伪造有明确定义；源布局阅读顺序、标题、表格表头/行/页/slide/sheet定位可回归；超长行切片保留表头/主键定位，空值不得补造。模型/源码/配置变更使指纹变化，路径移动但模型内容相同不应误报身份变化。

```python
def test_disabled_factory_needs_no_enhanced_client():
    parser = build_parser(ParsingConfig(), max_bytes=30 * 1024 * 1024)
    assert isinstance(parser, StructuredDocumentParser)
    parsed = parser.parse(SourceContent("通用资料".encode(), "说明.txt", "text/plain"))
    assert parsed.sections[0].text == "通用资料"
```
- [ ] **Step 2: 红灯**：`python -m pytest tests/document_parsing/test_adapter.py tests/document_parsing/test_router.py tests/document_parsing/test_normalizer.py tests/document_parsing/test_fingerprint.py -q`。
- [ ] **Step 3: 实施最小适配**：在现有安全预检之后路由；抽出可复用预检而不降低宏/加密/展开限制。使用 DoclingDocument 的结构化条目与表格数据转换，不从整段Markdown重猜表格，不补写事实。长表格切片只在需要的结构分支增加，保留原生普通段落行为。PDF/图片启用OCR及表格模型；Office不强制模型，明示图片未识别的warning。
- [ ] **Step 4: 绿灯及原生回归**：运行上述新测试加 `python -m pytest tests/test_parsers.py tests/test_chunking.py -q`；输出文本/定位证据，区分实际OCR与纯结构替身测试。
- [ ] **Step 5: 保存任务提交/差异**：通用测试样例使用“示例企业/示例产品”，不复制当前公司的产品字典与考核内容。

### Task 4: 接入同步与可验证的增量/失败/回滚语义

**Files:** Modify `src/knowseam/cli.py`、必要时 `src/knowseam/sync.py`；Create `tests/document_parsing/test_sync_integration.py`；Extend `tests/test_cli.py`、`tests/test_postgres_e2e.py`（仅本次新增用例）。不新增产品表或模型问答流程。

**Interfaces:** Consumes Task 3 的 build_parser/version 和 Task 2 的 ParserClient；沿用 SyncWorker/store.publish/KnowledgeService，不改变公开接口。增强客户端由当前 AsyncExitStack 生命周期释放；不访问额外业务数据库。

- [ ] **Step 1: 写失败测试**：sync/worker使用配置工厂；serve/status/init-db不连接解析器。相同源revision+指纹跳过，配置或源码身份变化重新解析，相同切片输入复用向量缓存；socket不可用、OCR失败和超时不发布空版本且旧active_version保留为stale；首次失败没有可检索版本。

```python
# test_docling_failure_keeps_old_version_stale 中，使用隔离case及已保存的before行。
assert after["active_version"] == before["active_version"]
assert after["status"] == "stale"
# test_docling_pipeline_change_reindexes_without_reembedding 中：
assert after["active_version"] != before["active_version"]
assert embedding_call_count_after == embedding_call_count_before
```
- [ ] **Step 2: 红灯**：`python -m pytest tests/document_parsing/test_sync_integration.py tests/test_cli.py -q`；新增需数据库用例先在后述隔离DSN执行。
- [ ] **Step 3: 实施工厂接入**：替换CLI硬编码构造，不触碰其他产品。保持现有pipeline_id中的模型/维度/tokenizer/chunker；覆盖增强身份与配置，允许首版较宽的重新处理。继续先刷新ACL、完整扫描才reconcile、最后原子publish；错误保持既有sanitize策略并增加非敏感阶段/错误分类日志，不记录文档全文或凭据。
- [ ] **Step 4: 绿灯与存储不变量**：运行上述测试；在隔离PostgreSQL上运行新增 `test_docling_pipeline_change_reindexes_without_reembedding`、`test_docling_failure_keeps_old_version_stale`、`test_rollback_uses_current_source_revision`、`test_revoke_or_delete_during_parse_cannot_publish`。复用现有 store 事务验证，发现必须改存储时只修改通用缺陷并记录理由，不改变业务授权。
- [ ] **Step 5: 保存任务差异及证据**：不运行生产 `sync --force`，不手动切换现有active_version；记录索引旧/新指纹、revision、向量调用数与失败状态，不宣称已完成生产迁移。

### Task 5: 源码部署、真实离线解析与通用端到端验收

**Files:** Create `deployment/Dockerfile.parser`、`deployment/compose.docling.yaml`、`scripts/prepare-parser-models.sh`、`scripts/parser-smoke.sh`、`tests/document_parsing/test_container_contract.py`、`test_real_formats.py`、`test_generic_e2e.py`、`tests/document_parsing/fixtures/README.md`及合法合成样例；Modify `deployment/compose.yaml`、`pyproject.toml`测试marker定义与仅测试使用的PyYAML依赖。

**Interfaces:**
- parser opt-in Compose profile `docling`，共享命名socket卷；parser/worker UID/GID=10001:10001。parser不加入网络、不挂载private配置/DB/Docker socket，只读模型目录。使用同仓库本地wheel，禁止PyPI下载替换实现。
- 初始CPU预算：parser并发1、模型线程2、cpus=2、mem_limit=4g、pids_limit=256、tmpfs `/tmp` 512 MiB；均为项目容器约束，不修改宿主配置。部署资源不足时报告，不挤占现有服务绕过。
- 模型准备脚本仅在人工执行时下载选定模型至明确项目目录，生成每文件SHA256和许可/来源manifest；解析运行离线。RapidOCR模型位于模型根下 `RapidOcr/`，布局/表格模型按所选版本定义准备。
- `tests/document_parsing/test_generic_e2e.py`：真实源Connector→解析容器→SyncWorker→PostgresStore/BM25→KnowledgeService/RRF/reranker→API→MCP消费者；使用确定性向量/重排序替身，不调用在线LLM。
- 数据库测试仅允许 `SUITE_KNOWLEDGE_TEST_DSN` 指向 dbname `knowseam_test`，必须通过既有 checked_dsn/current_database 双校验。没有隔离数据库则明确skip，不把skip计为通过。新临时数据库容器仅在项目测试Compose中创建，不使用现有业务DB超级权限。

- [ ] **Step 1: 写失败测试**：Compose解析器无网络/凭据挂载/暴露端口，关闭profile无解析容器；实际导入内容摘要匹配subtree；真实中文扫描图/PDF有可核验正文、表格关系与定位，空白图不声称OCR成功；缺失/损坏模型不尝试联网。负向测试验证容器看不到worker凭据、不能请求公网或其他内网服务、输入URL/路径被拒绝。

```python
def test_parser_container_has_no_source_credentials_or_network():
    service = yaml.safe_load(Path("deployment/compose.docling.yaml").read_text())["services"]["parser"]
    assert service["network_mode"] == "none"
    assert service.get("ports", []) == []
    assert service["read_only"] is True
    assert not any("/private" in str(item) or "docker.sock" in str(item)
                   for item in service.get("volumes", []))
```
- [ ] **Step 2: 红灯**：`python -m pytest tests/document_parsing/test_container_contract.py -q`；真实解析marker测试未具备资源时只记录blocked/skip，不把其当TDD红灯。
- [ ] **Step 3: 实施项目部署文件**：增强容器使用独立入口，不读取全部KnowSeam配置；模型和socket挂载不扩展其他容器权限。模型预置后构建测试镜像；compose overlay使用项目专用名称和隔离卷，不改变正在运行的suite-knowledge、A/B或其他Compose项目。
- [ ] **Step 4: 真实离线验收**：`python -m pytest tests/document_parsing/test_real_formats.py -m docling_real -q`；记录格式、模型/源码身份、正文断言、定位和耗时。确认断网真实处理，不用替身替代OCR证据。
- [ ] **Step 5: 通用端到端与权限回归**：设置只针对knowseam_test的测试DSN，运行新generic_e2e及现有Postgres全链路、API/MCP测试。复用Postgres `case/checked_dsn`，扩展ControlledFiles为显式公开的bytes样例；把实际KnowledgeService接入既有API/MCP transport scaffold，而不是继续使用业务服务double。演示两位合成员工访问全员/部门/指定员工资料，撤权即时生效；模拟源更新后回滚解析器、删除后回滚、读取分页与引用。严格不向真实员工发消息。
- [ ] **Step 6: 保存验收证据/提交**：仅清理本任务明确创建的测试容器/卷，精确核对项目label与绝对目标；模型保留供重复测试，临时测试数据可回收。记录哪些测试通过/skip/blocked，不能以健康检查替代原文与权限链路验证。

### Task 6: 维护文档、完整回归与发布交接

**Files:** Create `docs/DOCLING-MAINTENANCE.md`、`docs/DOCLING-VALIDATION.md`、`tests/document_parsing/test_docs.py`；Modify `README.md`、`config.example.json`（仅公共解析字段）、必要的 `.gitignore`（模型/wheel/cache，不忽略第三方源码）。

**Interfaces:** 文档给出普通clone/pull、维护人员subtree升级、源码定制、本地wheel构建、模型预置、启用/禁用profile、测试命令、失败诊断和回滚。准确区分开发完成、验收完成、生产启用。

- [ ] **Step 1: 添加文档契约检查**：示例配置可加载且默认关闭；维护脚本的ref/SHA/源码包名称与构建元数据一致；不含服务器私密配置、企业字典、真实资料或模型权重。

```python
def test_public_example_keeps_enhanced_parser_optional():
    example = json.loads(Path("config.example.json").read_text())
    assert example["parsing"]["docling"]["enabled"] is False
    assert "docling" not in Path("deployment/Dockerfile").read_text().lower()
```
- [ ] **Step 2: 完成文档**：升级必须在分支和干净worktree，不自动部署；冲突不覆盖源码；回滚重新处理当前revision并保留ACL；说明第三方源码不包含全部依赖源码。记录隔离解析容器与统一代码仓关系。
- [ ] **Step 3: 全量回归**：在隔离环境运行 `python -m pytest tests -q` 与 `python -m ruff check src/knowseam tests/document_parsing`。另显式运行真实Docling和隔离Postgres测试，列出marker和DSN导致的skip。执行现有SuiteHarness适配契约测试可以读其源码但不修改底座；不以产品A考核测试替代通用验收。
- [ ] **Step 4: 最终独立审查**：给审查者任务差异、spec、测试证据，核查可选依赖、vendored实际运行、受限容器、指纹、权限、API/MCP兼容、保留原有修改。修复本次引入问题并补覆盖测试；不进行无关优化。
- [ ] **Step 5: 发布交接**：交付源代码/补丁、锁文件、模型清单和验收报告；不擅自推送GitHub或修改生产配置。需启用开发服务器同步增强时，先给出准确镜像、配置差异、受影响KnowSeam服务、资源预算和回滚步骤供用户确认。产品A/B保持现有入口和服务状态。

## 基线依据与实施状态

- 发布：https://github.com/docling-project/docling/releases/tag/v2.135.0
- 固定源码：https://github.com/docling-project/docling/tree/2b3a00c5bfcd8b218d32e7e9c33d509aeaa0cb56
- 打包：https://github.com/docling-project/docling/blob/v2.135.0/pyproject.toml
- 离线模型：https://docling-project.github.io/docling/usage/advanced_options/

状态：实施计划待用户核对并选择执行方式。上述命令和用例是待执行项，不是已通过结果；尚未拉取第三方源码、安装依赖、写运行代码或切换服务。
