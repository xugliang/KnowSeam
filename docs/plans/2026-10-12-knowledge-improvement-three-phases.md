# KnowSeam 三批能力完善 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户已要求三批全部纳入规划；本文件交付不代表允许立即执行数据库迁移、启用新模型或切换生产服务。

**Goal:** 将 KnowSeam 完善为可复用的通用企业知识服务，系统补齐证据完整性、结构化读取、细粒度增量、权限生命周期、查询感知图谱、历史事实及视觉检索。

**Architecture:** PostgreSQL 继续保存权威文档、版本、授权与证据；BM25＋向量＋RRF＋重排序保留为基本链路，Neo4j 保持可重建派生投影。新增能力通过可选 ABC 接口和只读 MCP/HTTP 接入，不将会话、考核、销售业务或完整 Agent 循环搬入知识库。各批可独立验证、上线和关闭，但三批均属于最终交付范围。

**Tech Stack:** Python ≥3.11、现有 PostgreSQL/pgvector、Jieba/BM25、Pydantic/Starlette、MCP、可选 Docling/Neo4j；第三批视觉推理采用独立运行环境，不能升级或破坏现有文本模型环境。

**Spec:** 用户确认的“三批都做”；本文件第 1–8 节是自包含详细设计，第 9–13 节是执行任务、验收和切换计划。依据 2026-10-12 KnowSeam 改进调研，参考链接在第 14 节；不要求执行者访问其他会话。

**文档状态：** 用户已批准并执行开发；工程实现与隔离回归已推进，尚未全部通过真实模型、私有来源权限及规模验收，未执行生产切换。逐任务状态和证据以 [三批验收记录](../VALIDATION-THREE-PHASES.md) 为准；以下复选框保留原始验收清单，不表示已满足全部条件。
**权威仓库：** /srv/knowseam
**计划位置：** /srv/knowseam/docs/plans/2026-10-12-knowledge-improvement-three-phases.md
**代码核查基线：** 2026-10-12，服务器 HEAD bb2c42e，工作区有既有未提交修改；执行前必须重新核实。
**执行方式建议：** 主会话直接实施，独立模块可并行，接口/数据库迁移由主负责人统一整合；每批独立审查，最终整体审查。不自动向其他用户会话下发任务。

## Global Constraints

- 只在原 knowseam 仓库持续开发；不另建服务器开发 checkout，不改主机驱动、网络、Docker 配置或无关项目。
- 不修改 SuiteHarness 底座。必要的新底座契约另行说明理由并经用户确认；KnowSeam 自己的 harness_plugin.py 属知识库适配器，可以兼容扩展。
- 不改产品 A 考核、发题、答题、抄送规则，不改产品 B 客户数据；不向真实员工发送测试信息。
- 保留现有未提交改动、私有配置、企业术语、数据库与模型；不执行破坏性全库重建或删除历史。
- 代码完成、隔离测试通过、真实模型验证、生产启用是四个不同状态，不能互相替代。
- PostgreSQL 为权威，Neo4j/视觉索引为派生；派生索引不可绕过当前授权或代替原文事实。
- 可信 Principal 来自认证层，不接受模型自报 actor_id/company_id/权限；沿用公司、部门、指定员工集合权限。
- 新功能使用可选 ABC 能力组合，不向所有旧适配器增加必须实现的新抽象方法；缺能力显式 unavailable。
- 旧 knowledge_list/search/read 的名称、参数默认值与当前版本语义保留；新增工具均只读。
- 开发与验收可完整覆盖三批；生产默认开关保守关闭不代表没有开发。不得偷偷启用付费模型或下载权重。
- 新配置仍使用文件，不引入必需的配置中心、SaaS 控制台或第二套账号系统。
- 公开仓仅含通用代码、合成夹具、公开数据和去敏文档；凭据、私有词典、企业题库/客户信息、真实评测原文不得提交。
- 安全错误不回显 SQL 参数、鉴权头、原文、源 URL 或自由异常字符串；已有错误编号能力保留。
- 不把资源规模与模型效果作未经验证的承诺。本计划中的限额是拟定工程边界，不是已测性能。

## Review Focus

1. 查询、窗口扩展、表格读取、图遍历、页图下载过程中撤权或切换版本：返回前再次核验，不能泄漏旧证据。对应 T05、T07、T10、T12、T13、T16。
2. 新旧解析器/适配器并存：严格 v1 不被新字段击穿，新版可以处理旧输出；旧客户端不需要支持新工具。对应 T00、T06、T17。
3. 文本相同但表格类型、位置、词典、本体、模型或提示词改变：正确失效派生产物，缓存命中仍绑定新版出处。对应 T06、T08、T09。
4. 历史查询、迟到资料和冲突事实：不伪造过去已知时间，不用历史权限恢复已撤销访问，不按上传先后断定事实真假。对应 T13、T14。
5. 大表、巨型邻接图、重复文档和多向量页图：限额生效，权限内排序，降级明确，不能静默少算后宣称“全部”。对应 T03、T04、T07、T11、T12、T15、T16。

---

## 1. 范围、现状和三批交付

### 1.1 当前已有，不重复重做

- BM25＋向量双通道、RRF 融合（按排名合并）、reranker 重排序、输出前权限/版本复核。
- 标题/字符位置/表格行字段等 locator；Docling 多级表头、合并语义和标题祖先路径。
- 全文按块分页读取；搜索默认 8 条、最大 20 条，全文每页默认 50 块、最大 100 块。
- revision/SHA 增量判断、内容哈希向量缓存；同步过程已会观察权限，不是完全忽略权限变化。
- 可选图谱、来源引文、1–2 跳、关系有效期、事务 outbox、租约、检查点、最终路径复核和降级。
- 文档级 Recall/MRR、无答案误召回、P50/P95 的评测；尚不能据此宣称真实回答准确率已经达标。
- 日志/错误编号已有基础；需增加成功查询的分阶段诊断，而不是另造一个日志平台。

### 1.2 批次

| 批次 | 交付目标 | 任务 |
| --- | --- | --- |
| 第一批 | 找得全、读得完整、知道问题出在哪 | T00–T05：兼容/迁移基础、证据评测、诊断、目录、覆盖检索、上下文 |
| 第二批 | 表格可用、更新省成本、权限和性能可管理 | T06–T11：结构化表格、表格读取、块级索引复用、图抽取复用、来源权限、检索性能 |
| 第三批 | 关系、历史和复杂视觉资料可检索 | T12–T17：查询感知图谱、双时间事实、历史读取、视觉推理、视觉检索与资源、全链路验收 |

三批全部开发；不是做完第一批后默认取消后两批。阶段门槛用于阻止故障上线，不用于把未实现功能包装为完成。

## 2. 模块职责与调用链

### 2.1 职责划分

| KnowSeam 负责 | 调用方 Agent/产品负责 |
| --- | --- |
| 接入、解析、版本、索引、授权、增量一致性 | 用户意图、对话、业务角色 |
| 目录枚举、检索策略、上下文和表格/页图读取 | 选择策略、拆解问题、决定是否继续搜索 |
| 来源与证据定位、覆盖元数据、可选关系路径 | 综合原文、计算业务结果、面向用户回答 |
| 解析/检索/权限/时效评测 | 固定调用方的端到端答案质量 |
| 通用只读 MCP 与 HTTP | 考核、客户画像、销售建议等业务工具 |

服务不强制每次增加一次“意图识别 LLM”。首版由调用方显式选择策略，未选择则完全遵循旧行为；确定性策略选择、白名单过滤、预算控制在服务内完成。

### 2.2 目标调用链

~~~text
可信身份 → 授权范围/版本/时效检查
                ↓
      显式策略：legacy / coverage / relationship
                ↓
      BM25 + 向量 + 可选查询感知图
                ↓
      RRF → 重排序 → 去冗余/覆盖配额
                ↓
      当前权限、来源及支持路径再次校验
                ↓
      原文证据 + 定位 + 覆盖/降级状态
                ↓
调用方按需使用：目录 / 上下文 / 表格 / 历史 / 视觉工具
                ↓
       调用方综合回答并保留引用
~~~

视觉与历史首版使用独立工具；不直接改变旧 SearchHit 结构、不混用图像和文本分数、不把当前材料冒充历史证据。

### 2.3 代码组织

路径约定：Python 源模块均相对 `/srv/knowseam/src/knowseam/`，例如 config.py 的完整位置为 `/srv/knowseam/src/knowseam/config.py`；tests/、docs/、deployment/、scripts/、examples/ 和 pyproject.toml 相对仓库根目录。migrations/ 是包内目录，SQL 必须随包包含。后文沿用此约定；“新增”是计划，不表示文件已存在。

| 模块 | 现有修改点 | 拟新增文件 |
| --- | --- | --- |
| 兼容与迁移 | config.py、repository.py、storage.py、cli.py | capabilities.py、migrations/runner.py、migrations/sql/*.sql |
| 契约与证据 | domain.py、api.py、mcp_server.py、harness_plugin.py | contracts.py、evidence.py |
| 评测 | eval.py、cli.py、examples/evaluation.json | evaluation/cases.py、evaluation/metrics.py、evaluation/runner.py |
| 查询诊断 | service.py、retrieval.py、api.py | observability.py |
| 目录 | sync.py、storage.py | catalog/domain.py、catalog/service.py、catalog/postgres.py |
| 覆盖检索 | retrieval.py、service.py | coverage.py |
| 上下文 | chunking.py、sync.py、storage.py | context.py、context_store.py |
| 表格 | domain.py、parsers.py、document_parsing/* | tables/domain.py、tables/postgres.py、tables/service.py |
| 增量复用 | sync.py、storage.py、tokenization.py、graph/worker.py | indexing/diff.py、indexing/cache.py、indexing/postgres.py |
| 来源权限 | config.py、connectors/*、storage.py、graph/postgres.py | permissions/domain.py、permissions/store.py、permissions/sync.py |
| 性能 | storage.py、repository.py | scripts/benchmark_retrieval.py |
| 关系/时间 | graph/search.py、graph/domain.py、graph/postgres.py | graph/query.py、graph/facts.py、history/domain.py、history/service.py、history/postgres.py |
| 视觉 | document_parsing/*、config.py、cli.py | visual/domain.py、visual/encoder.py、visual/colqwen.py、visual/service.py、visual/postgres.py、visual/worker.py |
| 部署与文档 | deployment/*、README.md、pyproject.toml | docs/RETRIEVAL-CONTRACT.md、docs/UPGRADE-KNOWLEDGE.md、docs/VALIDATION-THREE-PHASES.md |

文件按功能形成可独立理解的 Module（模块）。不要把全部逻辑继续塞进 storage.py/service.py；只拆本次相关职责，不进行无关全仓重构。

## 3. 公共接口契约

### 3.1 不变部分

- knowledge_search：query 为非空字符串，最多 4096 字；limit 为整数 1–20，默认 8；拒绝 bool 冒充整数。
- REST collection_ids 为 null 时沿用认证允许范围；显式空数组拒绝；MCP 沿用必填非空列表，最多 32 个唯一 ID。
- knowledge_read：offset 为整数 0–1000000；limit 为 1–100，默认 50；next_offset=null 表示当前版本已入库内容读完。
- Principal 不在模型工具参数中。身份/集合校验需要同时存在于入口、数据库过滤和结果复核，不能只信 MCP 描述。
- 旧工具默认字段保持；新客户端可通过新增只读 GET /v1/capabilities 和 knowledge_capabilities 查询功能是否可用。
- 可用能力由已配置且就绪的适配器决定；“包已安装”“环境变量存在”不是能力就绪。
- 公共 Schema 为严格对象，未知字段、重复 JSON key、非有限数值拒绝；错误统一脱敏。

### 3.2 新增参数、工具与限额

以下是本计划确定的首版默认值；通过代码、HTTP Schema、MCP Schema、文档和边界测试统一维护，不允许各处限额不同。

| 工具/路由 | 输入及默认 | 返回与边界 |
| --- | --- | --- |
| knowledge_search / POST /v1/search | 新增 strategy=legacy，枚举 legacy/coverage/relationship；filters=null；include_diagnostics=false | 保留旧 hits/evidence_status/instruction；新策略才追加 coverage；legacy 不改变旧默认图配置 |
| knowledge_catalog / POST /v1/catalog | collection_ids；kind：配置白名单字符串 1–64；cursor=null；limit=50，范围 1–100 | items、next_cursor、catalog_status、scope；目录条目附当前授权来源，非全库隐藏计数 |
| knowledge_context / POST /v1/context | document_id/version_id；chunk_ids 1–20；mode=neighbors 或 section；before/after=1，范围 0–5；max_chars=12000，范围 1000–30000 | ranges、citations、context_status、truncated；只读当前版本；最多 100 个返回范围 |
| knowledge_table_list / GET /v1/documents/{id}/tables | version_id；offset=0；limit=50，范围 1–100 | tables、next_offset；只返回授权当前版本，不返回全部文件名猜测列表 |
| knowledge_table_read / POST /v1/documents/{id}/tables/{table_id}/read | version_id；row_start=1；row_limit=50，范围 1–100；column_ids=null 或 1–32 个唯一 ID | cells、columns、next_row、citations、warnings；至多 4096 单元格；列太多要求显式选择，不静默丢列 |
| knowledge_history_search / POST /v1/history/search | query、collection_ids、valid_as_of：必填带时区时间；known_as_of=null 表示当前认知；include_unknown_validity=false；limit=8，范围 1–20 | observations、citations、temporal_status；返回事实证据，不生成历史故事 |
| knowledge_history_read / POST /v1/history/read | document_id/version_id；known_as_of=null 或带时区时间；offset/limit 同旧 read | 历史原文和历史元数据状态；当前 ACL 仍必须通过；非空时间不得读取此后才接受的版本；不用旧 read 的布尔旁路 |
| knowledge_visual_search / POST /v1/visual/search | query、collection_ids；kind=null 或 page/table/chart；limit=8，范围 1–20 | assets、citations、visual_status；文字摘录区分原文/派生；不伪造普通文本 SearchHit |
| knowledge_asset_read / GET /v1/assets/{asset_id} | version_id；rendition=thumbnail 或 original | 鉴权字节流/MCP 受控资源；禁止任意路径/URL；每次读取重新授权 |

公共 JSON 默认响应上限 512 KiB；表格、窗口、诊断三者都计入。达到预算返回明确 truncated 或 response_too_large，不返回不完整 JSON。图片单次最多 10 MiB，走独立资源读取而非塞进搜索 JSON。所有新工具保持只读，不暴露同步、权限修改、Cypher 或 SQL 执行。

MCP 资源读取必须获得与原工具调用相同的可信身份，并调用上游资源鉴权接口；无法支持资源鉴权的客户端只返回“当前客户端不支持受控图片读取”，不能回退永久裸链接。

### 3.3 覆盖与目录语义

- filters 只允许配置登记的 kind、topic 和 document_type；每项最多 32 个值，值最长 128 字符。不接受 SQL、正则表达式、自由 where 或员工身份。
- coverage 策略每文档默认最多 2 个块；该值来自服务配置，范围 1–20，不让模型无限加大。
- 先在授权候选内重排，再控制同文档配额和高度重叠证据；最多使用既有 candidate_limit 范围 1–100。
- 回答“有哪些”优先枚举目录，而不是把 Top-K 当完整列表。目录只接受可信配置或来源元数据；企业词典里的词不自动成为真实产品。
- 目录完整性需要登记来源范围且成功完成扫描。catalog_status 为 complete/partial/unavailable；complete 仅指声明的数据源范围完整，不代表真实世界已穷尽。
- coverage.complete 使用 true/false/null：true 仅用于可核实的目录范围全部覆盖；已知尚未覆盖为 false；普通语义查询无法判断为 null。
- cursor 是不透明、有完整性保护的游标，绑定身份范围摘要、过滤条件、目录版本和授权版本；有效期 10 分钟。范围或版本变动返回 cursor_stale，要求重开。
- 撤权剔除结果后重新计算覆盖。隐藏文档的名称、数量、主题、冲突和候选计数不得对用户可见。
- 不为了“每个产品都有一条”而塞入无关材料；无法补齐时说明 partial。

### 3.4 内部 ABC 能力接口

不扩张旧 KnowledgeRepository 的必选方法集合。新增可选能力由构造函数显式注入；未配置为 None，调用时返回 capability_unavailable。Postgres Adapter 实现所启用的能力。

~~~python
# 类型定义于 contracts.py / 对应领域模块，均为有界、可校验的值对象。
class CatalogReader(ABC):
    @abstractmethod
    def list_catalog(self, principal: Principal, request: CatalogRequest) -> CatalogPage: ...

class ContextReader(ABC):
    @abstractmethod
    def read_context(self, principal: Principal, request: ContextRequest) -> ContextResult: ...

class TableReader(ABC):
    @abstractmethod
    def list_tables(self, principal: Principal, request: TableListRequest) -> TablePage: ...
    @abstractmethod
    def read_table(self, principal: Principal, request: TableReadRequest) -> TableResult: ...

class HistoryReader(ABC):
    @abstractmethod
    def search_history(self, principal: Principal, request: HistorySearchRequest) -> HistoryResult: ...
    @abstractmethod
    def read_history(self, principal: Principal, request: HistoryReadRequest) -> HistoryDocument: ...
~~~

同步数据库方法沿用 asyncio.to_thread 调度；模型方法为 async。新增 ABC 与两个实际实现（PostgreSQL 和确定性测试替身）一起落地；不预造十种未来数据库 Adapter。

## 4. 数据模型、迁移与版本发布

### 4.1 增量表

全部表名以 sk_ 开头，与当前风格一致。每个 document/version 外键均校验所属公司/集合；不能仅凭裸 chunk_id/table_id 跨文档读取。

| 迁移 | 表/字段 | 关键约束 |
| --- | --- | --- |
| 001 结构与目录 | sk_schema_migrations；sk_sections；sk_chunk_spans；sk_catalog_items；sk_catalog_evidence | 迁移 ID＋checksum；Section 以 version_id＋section_id 唯一；证据跨度为原始节 Unicode 字符左闭右开；目录证据必须绑定版本 |
| 002 类型化表格 | sk_tables；sk_table_cells | version_id＋table_id；单元格 1-based 行列，跨度≥1、不能越界；主单元格唯一 |
| 003 索引复用 | sk_token_cache；sk_term_payloads；sk_graph_extraction_cache；sk_derivation_dependencies；sk_index_revisions；sk_term_postings | 内容与证据身份分离；缓存带完整语义指纹；新版关联不沿用旧 locator/ACL |
| 004 来源权限 | sk_document_acl；sk_document_grants；sk_source_subject_bindings；sk_permission_observations | company/source 域隔离；权限 revision 与正文 revision 分离；单调观察序号/CAS 防迟到复权 |
| 005 历史事实 | sk_fact_observations；sk_fact_supports；sk_fact_events；sk_version_metadata | 双时间、事实与多来源支持分离；新版本保存当时标题/URL快照；事件幂等 |
| 006 视觉 | sk_visual_assets；sk_visual_embeddings；sk_visual_jobs | asset 与版本绑定；内部资源键受控；模型/预处理指纹；任务租约、重试、序号隔离 |

迁移不是启动时自动执行。新增命令 migrate --dry-run、migrate --apply；使用数据库 advisory lock，记录校验和，重复执行不重复写入。大索引 concurrent 建设单独执行，不塞进普通迁移事务。迁移失败保留旧表和活动版本，不通过删除表“回滚”。

### 4.2 结构证据

- Section 保存原始解析文本和标题路径，不把用于 embedding 的补充表头/标题冒充原文。
- chunk_span 保存 block 对原节的一个或多个范围。重叠切块、合并表头等情况不能假设 chunk.text 与原节偏移一一对应。
- 证据锚点包含 document_id/version_id/section_id、字符范围；表格另含 table_id/row/column；视觉另含 asset_id/page/bbox。
- bbox 采用明确坐标体系及页宽高，规范化到左上原点 [0,1]；校验有限数和有序边界，原始坐标及转换指纹可保留。
- 旧索引没有父节时，只提供已有邻块或 capability_unavailable；不得通过拼接增强文本声称恢复原文。

### 4.3 原子发布与回填

- 新内容版本先完成解析、结构/块/词项准备，在同一 PostgreSQL 发布事务中写齐证据并切 active_version，事务中同步入队图/视觉任务。
- 表格、父节与块不能分别发布导致半套版本。查询期间版本变化，旧请求按旧工具既定规则返回 unavailable/重试，不偷偷混入新块。
- 附加表格/视觉回填从经过 raw_hash 校验的原始归档重新解析。原文件缺失则标记 unavailable，并记录恢复方式；不从扁平文本伪造单元格类型。
- 回填默认批大小 20 文档，支持续跑与取消，默认并发 1；发布前再次检查内容 revision、解析指纹和权限版本。
- 旧版保留为可恢复证据；计划不自动清除旧数据。新增 GC 必须另有明确保留策略和备份授权。
- 所有内容/索引/授权版本称为 revision 或 sequence；不增加产品“generation”生命周期设计。

## 5. 表格与解析协议

### 5.1 结构化单元格

ParsedDocument 尾部新增默认空 tables 和 assets，不破坏旧位置参数。ParsedTable/Cell 使用以下语义：

- table_id 只在文档版本内唯一；列使用稳定 column_id，不能用可能重复的显示表头当唯一键。
- row/column 从 1 开始；row_span/column_span 表示合并范围，只存主单元格，读取覆盖区返回主格引用。
- value_kind：empty/text/number/boolean/date/error/unknown；保留 raw_text。空单元格、空字符串、数值 0 必须区分。
- 精确数值使用十进制字符串，不通过 JSON float 损失大整数、小数或负号；前导零编号保留文本。
- unit 来自明确表头/原格或可审计规则，不能由模型猜测。
- formula 原文可保存；formula_state 为 not_formula/cache_present/cache_missing/unknown；禁止执行公式、宏和外部链接。
- XLSX 配对读取公式与缓存视图；PDF/截图无法可靠确定类型时保留 unknown，不伪造计算值。
- 返回相邻行不等于覆盖全表；next_row/null 与截断状态明确，数值汇总由调用方完成。

### 5.2 v1/v2 兼容

当前 ParserIdentity.protocol_version 严格为 1，decode_document 拒绝额外字段。因此实施：

1. 新客户端支持 v1/v2 握手；v1 请求和响应完全维持旧字段。
2. v2 明确声明 tables/assets manifest；旧客户端必须继续收到 v1，不能直接多塞字段。
3. 新客户端收到 v1 时 tables/assets=空，但状态表明能力未提供，不代表文档没有表。
4. 更新 parsed_hash 和 pipeline 指纹，表格类型/单位/资产结构变更也触发更新。
5. 保留已有 30 MiB 输入、150 MiB 解析输出上限。每文档至多 1000 表、200000 单元格；超限显式拒绝增强输出，不静默丢表。
6. 页图通过沙箱内部受控资源转交，主服务不访问任意解析器返回路径；文件必须在任务输出目录、类型/大小/hash 验证通过。

## 6. 增量、权限和性能

### 6.1 两层增量，不把缓存命中当作证据发布

**计算复用：**

- 向量沿用现有输入 hash 缓存。
- 分词缓存键包含 company、tokenizer/词典/企业术语指纹及实际输入 hash。
- 图抽取缓存限公司＋集合内，键包含实际抽取文本 hash、模型 ID/revision、endpoint identity、提示词、输出校验规则、本体/aliases、解析语义指纹。
- 缓存不含文档版本、locator、权限或 EvidenceRef；新版本命中后重新做 quote 精确校验，并绑定新证据。
- 不固定模型 revision 且服务可能热换模型时，使用显式 cache_revision；未设置则关闭跨版本模型缓存。

**索引写入复用：**

- sk_chunks 保留版本化证据定位。sk_token_cache 以 company＋tokenizer_fingerprint＋input_hash 标识不可变载荷并保存总词数；sk_term_payloads 按该载荷标识＋term 唯一保存 tf（词频）；sk_term_postings 以 version_id＋chunk_id 唯一关联载荷。相同内容/分词指纹复用词项载荷，新版只写关联。
- 以版本片段顺序＋内容 hash 匹配变更，不把 ordinal 当跨版本永久身份；重复内容允许共享计算但保留不同证据位置。
- 新 postings 通道与旧 sk_chunk_terms 并行验证，按索引 revision 原子切换；回滚旧读取仍可用。
- 图第二批保持文档级原子替换，不立即强行引入复杂逐边写入协议；通过跨版本缓存减少抽取成本，并记录受影响来源依赖。
- 如果只完成缓存、未减少重复词项载荷写入，T08 不算全部完成；SQL 写入减少和模型调用减少分别报告。

### 6.2 来源 ACL 独立同步

ACL 是“谁能看哪些资料”的访问控制清单。

- 旧模式 public_verified：继续要求来源明确全员可读。机器人可以打开文件不等于所有员工都有权限。
- 新模式 source_acl：集合权限与真实来源用户/组权限取交集；来源用户/组与内部员工通过可信映射关联，不让模型自报组成员。
- verified/unknown/revoked 三态；null 表示未获得完整结果，空 grants 表示已核实没有授权，不能混淆。
- 内容同步已观察权限，新增的是独立 permissions-worker/permissions-sync，使解析或模型故障时仍能撤权；不得启动 embedding 模型。
- 初始间隔 300 秒，配置范围 30–3600 秒且必须小于 acl_ttl_seconds；启用时若现有 TTL 不满足则报错要求调整，不擅改配置。初始 TTL 沿用现值。
- 权限观察调用前记录单调 sequence，写回用文档行锁/CAS；迟到响应不能覆盖较新撤权。不按响应抵达时间判断新旧。
- 正文扫描 observe、内容 fetch 前后复核与独立 permissions-worker 必须使用同一 PermissionStore 和 sequence 规则；publish/touch_unchanged 只检查最新 ACL revision，不能用旧响应刷新 public/acl_verified_at。不能只保护新 worker、留下旧同步入口覆盖撤权。
- 图/视觉任务准入按来源模式和权威 ACL 状态判断；source_acl 下合法私有资料仍保持 public=false，但可生成受控派生产物，不能继续用 public=true 作为投影唯一准入条件。各用户查询时仍分别授权。
- 分页不完整、来源不可达、映射过期时不扩大授权；明确撤权立即生效，未知状态 fail closed，错误日志仅类别。
- 图/视觉物理清理可异步，但 PostgreSQL 授权立即阻止读取。授权更新不需要重新计算向量。
- 飞书适配器第一步复用既有公开权限检查；私有用户/组/继承语义必须用官方 API 与完整性测试验证后才声明支持。不支持的文件类型返回 unsupported，不以手工缓存冒充自动同步。
- 已存在私有文档时，回滚旧二进制前禁用相应来源，保持 public=false；不能为了兼容把受限文档变成公开。

### 6.3 性能策略

- 保留授权子集内的 N、df、avgdl；隐藏资料不能改变用户可见 BM25 分数、排名、目录计数或冲突状态。
- 首轮优化为必要索引、批量读取和不重复装载原文；不先引入 Elasticsearch 等新必需数据库。
- 统计缓存只有在 key 覆盖授权文档版本集合、员工/组授权 revision、内容/模型/分词指纹以及 TTL 边界时才能启用。
- ANN（近似向量搜索）不是默认验收结果；不能全库先取 Top-K 再过滤权限。仅对完全授权的检索分区采用经过验证的 ANN，否则保留 exact。
- 所有 EXPLAIN ANALYZE、规模负载测试运行在隔离测试库，不在贵重生产库压测。
- 规模基准包含 1k/10k/100k 授权块及大量隐藏块，报告冷热 P50/P95、执行计划、CPU/内存、索引体积和吞吐；这是工程验收，不是把通用产品降为小规模原型。

## 7. 查询感知图谱与历史事实

### 7.1 图谱策略

内部 GraphSearch.expand 保留原三个位置参数，新增 query=None、options=None；旧调用仍有效。GraphQueryOptions.strategy 为 seed/query_aware，默认 seed；include_paths=false；hops 仍为 1–2，候选仍为 1–100。

- relationship 请求仅在 query_aware.enabled 时启用；不开启时返回明确能力未启用，不悄悄调用额外 LLM。
- 首版按配置实体别名、本体关系和 query 相关性筛选；不硬编码产品名称或仅靠中文关键词决定所有查询路径。
- PostgreSQL 维护可授权的事实与来源关联，先在授权候选中筛选/排序，再传显式事实集合给图遍历。不能沿用全图排序 LIMIT 后才过滤的方式作为新模式。
- 对每跳和最终整个路径重新授权；解释只含可访问的来源 quote/locator、关系类型、派生标记。隐藏桥接关系不能通过数量或排序泄漏。
- 新查询能力使用独立 QueryGraphStore ABC，不给旧 GraphStore 增加必须实现的方法；Neo4j Adapter 与测试替身实现。
- 图不可用只影响可选图通道，基础检索继续；不能返回假“图谱成功”。

### 7.2 双时间与冲突

**valid_from/valid_until：** 事实在现实中何时有效，来自明确原文；不知道则为 null，并标记 unknown。

**recorded_from/recorded_until：** KnowSeam 从何时起接受该断言、何时撤销该认知，来自权威事务时间。

- 区间统一左闭右开，有两个边界时 until 必须大于 from。旧零长区间标记待核验，不自动修改原文。
- 首次迁移抽取旧材料，不将系统认知时间倒填为材料日期。已有可核验接受事件才能据此回填。
- 事实规范键与来源支持分开；同一事实可有多份支持，删除一源不删除其他仍合法支持。
- 冲突依据可选本体约束：同主语、单值关系、限定条件相同、有效期重叠、对象不同。多值关系不自动判冲突。
- 新上传不自动推翻旧断言；明确更正或管理员审计操作才设置 supersedes。模型分数不能决定覆盖。
- 冲突只在当前用户可见证据中计算；隐藏材料不能使用户看到“存在一个秘密冲突来源”。

### 7.3 历史读取是独立契约

- 普通 read/search 继续要求活动版本，不能删掉 active_version 条件来“支持历史”。
- 历史查 valid_as_of 时，只返回明确该时点有效的事实；include_unknown_validity=true 才附带独立标记的未知时间事实。
- known_as_of=null：按现在已知材料回看过去；非空：进一步要求系统当时已知。
- 历史原文需要文档现在仍可读、当前来源启用、ACL 新鲜、员工有效、历史版本确属该文档且未删除/清除；历史权限不能覆盖当前撤权。
- history_read 的 known_as_of 非空时，版本必须具有可核验的 accepted_at 且 accepted_at≤known_as_of；这是系统首次接受该版本的时间，不是来源发布时间或事实有效时间。缺可靠记录返回 temporal_unverifiable，晚于查询时点返回 version_not_known_at_time。搜索命中与后续读取沿用同一 known_as_of，不能用后来版本全文补齐历史材料。
- 新版本保存当时标题/URL；旧版本无快照时标 metadata_incomplete，不能把当前标题伪装成过去标题。
- 第一实现由 PostgreSQL 历史事实/支持表检索，避免要求 Neo4j 同时维护所有历史。可选历史图投影可由权威事实重建，独立 projection_key，不能破坏当前 replace_document。
- 关闭 history 只隐藏功能，不删除事实档案；保留时间默认无限/无自动删除，清理需后续明确保留策略和授权。

## 8. 视觉能力与模型隔离

### 8.1 交付范围

- 页图、表格区域、图表资产的生成、授权存储、视觉向量、检索及受控读取完整实现。
- 首版支持 PDF/图片以及解析器可可靠输出的页/区域；PPT 等无法渲染的格式明确 capability 状态，不虚报“全格式”。
- 文本、表格结构与图像可互相定位；数值优先读取结构化 cell。找对图不等于读对数字。
- 输出 caption/OCR 必须标 derived，不能当作人工原文；图片中的指令是资料，不改变工具权限。

### 8.2 模型方案

首个拟实现适配器采用 ColQwen2 路线（vidore/colqwen2-v1.0）作为可替换的视觉检索基线，而非宣称它是最新或最优中文模型。模型卡说明其输出是多向量，主要检索训练资料为英文；必须进行中文企业表格验证。

- 使用独立 visual worker/runtime，固定权重 revision、包锁文件、预处理和查询格式。现有文本模型约束 sentence-transformers<6，不能为视觉模型升级原环境。
- VisualEncoder ABC（均为 async 方法）：
  encode_queries(texts: Sequence[str]) -> list[MultiVector]；
  encode_assets(assets: Sequence[VisualAsset]) -> list[MultiVector]。
  MultiVector 明确向量数、维度及有限浮点验证，不冒充现有单向量 Embedder。
- 第一存储实现用 PostgreSQL 保存受控页图信息、粗召回表示及分块多向量载荷。先在授权范围做粗召回，再对最多 100 个资产做 MaxSim（查询各向量与页图向量匹配后汇总）排序。
- 对照授权全集精确 MaxSim 衡量粗召回损失；不达标时不能以近似结果宣称完整。保留精确基线，VisualRetrievalBackend 可替换，不新增强制数据库。
- 视觉每文档资产上限 500，每图最长边 2048、最多 16M 像素、编码字节≤10 MiB；模型自身更严限额取交集。超限显式报错/分页，不静默遗漏。
- visual.enabled=false、allow_online=false；权重目录不存在、指纹不匹配、依赖缺失时配置校验失败，禁止伪向量和隐式下载。
- 模型、权重、依赖许可证分别核查，保存 lock/NOTICE/模型 hash。资源安排以 开发服务器 可用显存/CPU 实测为准；不得占用无关 GPU 任务或改驱动。
- 开发完成至少需要一次真实视觉推理、真实 PG 检索与资源授权验证；只有替身测试时标为“实现待真实模型验收”，不能报完成。

## 9. 执行任务清单

### 通用执行规则

下列路径按 §2.3 约定解析。任务编号是稳定标识，不表示必须按编号执行；以“依赖”和 §10 为准，第三批先 T13 事实存储，再 T12 图检索。每项均按以下顺序：先实现测试与预期断言 → 运行确认因缺少能力失败 → 最小实现 → 目标测试通过 → 独立检查差异与文档 → 仅提交本项文件。禁止 git add . 混入用户改动，不在本计划执行中自动 push。

测试命令中的 python 指执行前确认的隔离测试解释器，不是系统 Python。当前已发现可复用 .build-public-release-20261010/test-env/bin/python；依赖需先核实。测试文件标记“新增”时当前不存在，首次失败应是能力缺失而非错误依赖、错误数据库或拼错路径。

### T00：契约、开关与显式迁移基础

**依赖：** 无。**文件：** 修改 config.py、cli.py、storage.py、repository.py、仓库根 pyproject.toml；新增 contracts.py、capabilities.py、migrations/runner.py、migrations/sql/001_structure.sql；测试 tests/test_capabilities.py、tests/test_migrations.py。

**Interface：** CapabilitySet 提供 catalog/context/tables/history/visual/query_graph 能力；get_capabilities(principal) 只返回该身份可用能力。MigrationRunner.plan()/apply() 返回版本与校验和状态，不加载模型。

- [ ] 写 test_legacy_adapter_needs_no_new_abstract_method、test_capability_disabled_no_optional_import、test_migration_resume_and_checksum_mismatch；断言旧 Adapter 可实例化、关闭时无 Neo4j/视觉 import、重复迁移不重复写。
- [ ] 运行 python -m pytest tests/test_capabilities.py tests/test_migrations.py -q，确认新增行为先失败。
- [ ] 实现能力注入、文件配置默认值、迁移锁与 dry-run/apply；配置 SQL 包资源并验证源码/构建产物都能读取迁移文件；不得启动自动回填。
- [ ] 同命令通过，并以真实 knowseam_test 验证事务失败保留原 schema/data。
- [ ] 独立检查 Schema、命令帮助和默认配置；仅提交 T00 文件，记录 commit ID。

### T01：证据级评测与基线

**依赖：** T00。**文件：** 修改 eval.py、cli.py；新增 evaluation/cases.py、evaluation/metrics.py、evaluation/runner.py、tests/test_evidence_eval.py、tests/fixtures/evaluation/v2.json。

**Interface：** EvaluationCaseV2 保留 expected_document_ids，新增 case_type、principal_ref、snapshot_ref、required_evidence_groups；组间 AND、组内 alternatives 为 OR。EvidenceSpan 与 §4.2 一致。旧 eval.py 导出保留兼容包装。

- [ ] 写 test_wrong_span_same_document_is_not_complete、test_alternative_evidence_satisfies_one_group、test_v1_evidence_metric_is_null、test_private_labels_not_exported；断言同文档错段不算完整，重复块不刷分。
- [ ] 运行 python -m pytest tests/test_eval.py tests/test_evidence_eval.py -q，确认新增断言先失败。
- [ ] 实现文档 Recall、证据组 Recall、完整支持集命中率、表格字段/数值证据指标；错误案例计入失败，非静默丢弃。deployed k≤20，大于 20 仅显式离线 retriever 支持。
- [ ] 同命令通过；建立普通事实、枚举、跨段、多文档、冲突、无答案、术语、表格、权限、增量十类夹具。
- [ ] 固定私有质量集和索引/身份/模型/预算清单；公开报告不含 query、正文、ID、URL。保留基线结果，不凭合成替身宣称真实模型质量。
- [ ] 独立检查标签和指标含义；提交本项代码与合成案例，不提交企业标签。

### T02：成功请求的阶段诊断

**依赖：** T00。**文件：** 新增 observability.py、tests/test_retrieval_trace.py；修改 service.py、retrieval.py、api.py、config.py。

**Interface：** RetrievalTrace/StageMetric 记录 request_id、phase、duration_ms、授权候选数、预算/降级枚举；Observer 默认 NoOp，可插拔。

- [ ] 写 test_trace_success_stage_order、test_trace_sink_failure_does_not_fail_search、test_logs_no_query_or_credentials、test_hidden_candidates_not_exposed。
- [ ] 运行 python -m pytest tests/test_retrieval_trace.py -q，确认先失败。
- [ ] 实现 auth/embed/lexical/dense/graph/fusion/rerank/coverage/revalidate 阶段；日志默认去敏，诊断权限不得从请求 bool 推导。
- [ ] 同命令通过；无 sink 时基础请求可用，有 sink 故障不能拖垮请求；错误编号能关联阶段。
- [ ] 更新 docs/RETRIEVAL-CONTRACT.md 并提交。Phoenix 只是可选消费方，不是运行依赖。

### T03：有来源的通用目录

**依赖：** T00、T02。**文件：** 新增 catalog/*、tests/test_catalog.py、tests/test_catalog_postgres_e2e.py；修改 sync.py、config.py、api.py、mcp_server.py、harness_plugin.py。

**Interface：** CatalogReader.list_catalog → CatalogPage；CatalogItem 含 kind/item_id/name/aliases/metadata 和授权 EvidenceSpan 集合；来源配置可登记 completeness scope。

- [ ] 写 test_catalog_not_inferred_from_top_k、test_term_dictionary_not_catalog、test_cursor_bound_to_identity_and_revision、test_catalog_revocation_and_partial_scan。
- [ ] 运行 python -m pytest tests/test_catalog.py tests/test_catalog_postgres_e2e.py -q，确认先失败；真实 PG 跳过不算此任务验收。
- [ ] 实现持久目录、来源引用、授权枚举、游标、状态；显式无目录返回 unavailable，不编造产品全集。
- [ ] 同命令通过；多页枚举覆盖全部可见项，隐藏项增删不影响可见计数，扫描中断不误报 complete。
- [ ] 同步 MCP/HTTP/适配器 Schema 与工具描述，提交；不把公司术语写入公共默认配置。

### T04：覆盖检索与兼容策略

**依赖：** T01–T03。**文件：** 新增 coverage.py、tests/test_search_coverage.py；修改 retrieval.py、service.py、config.py、api.py、mcp_server.py。

**Interface：** SearchOptions(strategy,filters,include_diagnostics)；select_coverage(hits, catalog_scope, max_per_document, limit) -> CoverageSelection。不得修改默认 legacy。

- [ ] 写 test_legacy_output_compatible、test_one_long_document_does_not_fill_all_slots、test_missing_catalog_complete_unknown、test_revoke_then_recompute_coverage。
- [ ] 运行 python -m pytest tests/test_search_coverage.py tests/test_retrieval.py -q，确认新增能力先失败。
- [ ] 实现重排后的文档配额/重叠范围去冗余/目录覆盖，排序分数相同按稳定证据键决胜；最终权限过滤后计算 coverage。
- [ ] 同命令通过；固定问题做 legacy/coverage 对照，普通事实不能因人为覆盖混入无关材料。
- [ ] 更新旧客户端和新客户端契约测试，提交；不通过扩大全局 limit 冒充覆盖优化。

### T05：父节存储与按需上下文回取

**依赖：** T00、T01、T02。**文件：** 新增 context.py、context_store.py、tests/test_context_structure.py、tests/test_context_read.py；修改 domain.py、chunking.py、sync.py、storage.py、service.py、api.py、mcp_server.py。

**Interface：** ContextReader.read_context → ContextResult；持久化原始 Section 与 chunk_span；新旧发布入口兼容，增强写入使用同一事务。

- [ ] 写 test_original_section_not_embedding_prefix、test_merge_neighbor_windows_without_duplicate_evidence、test_legacy_index_cannot_fake_parent、test_context_revoked_before_return。
- [ ] 运行 python -m pytest tests/test_context_structure.py tests/test_context_read.py tests/test_chunking.py -q，确认先失败。
- [ ] 实现 §3/§4 的结构发布、窗口合并、预算、引用；旧索引仅邻块降级，不自动重解析全库。
- [ ] 同命令通过；真实 PG 注入发布失败、查询期间版本替换、同名文档跨公司、超长节与重叠块。
- [ ] 记录第一批验收与可关闭开关，独立审查通过后提交。

### T06：类型化表格与解析协议 v2

**依赖：** T00、T05。**文件：** 新增 tables/domain.py、tests/test_table_contracts.py；修改 domain.py、parsers.py、chunking.py，以及 document_parsing/ 下的 protocol.py、normalizer.py、client.py、process_runner.py、docling_adapter.py、extensions/tables.py。

**Interface：** ParsedTable/TableCell/VisualAssetManifest 类型；ParserIdentity 协商 v1/v2；decode_document 按协商版本严格校验。

- [ ] 写 test_v1_strict_and_v2_negotiated、test_leading_zero_and_decimal_precision、test_formula_cache_missing_no_execution、test_table_only_change_invalidates_parsed_hash。
- [ ] 运行 python -m pytest tests/test_table_contracts.py tests/document_parsing/test_protocol.py tests/test_parsers.py -q，确认先失败。
- [ ] 实现 §5 的单元格语义与 v2 兼容；不改第三方 vendored 源码核心行为，优先 KnowSeam adapter/normalizer 扩展。
- [ ] 同命令通过；真实 Docling/XLSX/PDF 校验多级合并表头、偏移 sheet、重复列名、空字符串、日期、缓存公式、未知类型。
- [ ] 保存公开合成格式夹具、依赖/模型指纹；提交。仅替身通过不能称真实解析兼容完成。

### T07：版本化表格存储和只读工具

**依赖：** T06、T02。**文件：** 新增 tables/postgres.py、tables/service.py、migrations/sql/002_tables.sql、tests/test_tables.py、tests/test_table_postgres_e2e.py；修改 storage.py、sync.py、service.py、api.py、mcp_server.py、harness_plugin.py。

**Interface：** TableReader 的 list/read；表格随版本发布事务提交；输出以当前授权版本为准。

- [ ] 写 test_table_publication_rolls_back_atomically、test_table_pagination_no_lost_rows、test_merged_cell_anchor_read、test_table_acl_changes_between_read_and_return。
- [ ] 运行 python -m pytest tests/test_tables.py tests/test_table_postgres_e2e.py -q，确认先失败。
- [ ] 实现限行列、精确值、单位和引用；拒绝任意 SQL/公式执行；响应不足预算时明确 next_row/truncated。
- [ ] 同命令通过；端到端 HTTP/MCP 读取合成价格表并由测试调用方确定性求和，验证来源和数字不漂移。
- [ ] 更新表格使用说明并提交；统计业务策略不写进 KnowSeam。

### T08：块级差分与词项载荷复用

**依赖：** T00、T01、T05。**文件：** 新增 indexing/diff.py、indexing/cache.py、indexing/postgres.py、migrations/sql/003_index_reuse.sql、tests/test_index_diff.py、tests/test_index_reuse_e2e.py；修改 sync.py、storage.py、tokenization.py。单元开发可与 T06/T07 并行；应用 003 的集成验收必须已有 T07 的 002 schema，不允许跳号应用。

**Interface：** plan_diff(old_spans,new_spans) -> ChunkDiff；TokenCache.get/put 使用规范 hash；publish_index_revision 在内容版本事务中发布关联。

- [ ] 写 test_insert_at_start_preserves_unchanged_payloads、test_duplicate_content_keeps_distinct_locators、test_dictionary_change_invalidates_tokens、test_index_flip_atomic_and_legacy_rollback。
- [ ] 运行 python -m pytest tests/test_index_diff.py tests/test_index_reuse_e2e.py -q，确认先失败。
- [ ] 实现不可变词项载荷、版内 postings 和差分；保持 sk_chunks 证据、模型 hash、当前版本原子切换；构建旧词项通道兼容回填/保留。
- [ ] 同命令通过；一处修改只新增对应计算/词项载荷，新旧 BM25 结果等价；分别报告 SQL 行数、向量/分词调用数。
- [ ] 提供显式迁移和回退命令/日志，提交。不承诺完全不写新版本证据映射。

### T09：跨版本图抽取复用与依赖审计

**依赖：** T08。**文件：** 修改 graph/worker.py、graph/extraction.py、graph/postgres.py、graph/config.py；新增 indexing/dependencies.py、tests/test_extraction_cache.py、tests/test_derivation_lifecycle.py。

**Interface：** ExtractionCache 返回无 EvidenceRef 的 ChunkExtraction；validate_for_chunk(chunk, cached, ontology) 在每次命中执行；依赖表连接派生产物与源版本/块。

- [ ] 写 test_only_changed_text_reextracted、test_cache_rebinds_new_locator、test_prompt_model_ontology_change_invalidates、test_shared_fact_keeps_remaining_support、test_cache_hit_during_revocation。
- [ ] 运行 python -m pytest tests/test_extraction_cache.py tests/test_derivation_lifecycle.py tests/test_graph_worker.py -q，确认先失败。
- [ ] 实现精确输入/语义指纹缓存、quote 复核、新版 checkpoint 与依赖审计；保留租约/claim_token/序号 fencing 和至多三次失败尝试。
- [ ] 同命令通过；真实 PG＋Neo4j 覆盖重复/乱序事件、任务中断、两文同事实、源更新/删除，不把用户撤权等同全局物理删除。
- [ ] 报告抽取调用节省、缓存失效和投影一致性，提交；不宣称已实现逐边差分写入。

### T10：来源 ACL 与独立权限同步

**依赖：** T00、T02、T07、T09。**文件：** 新增 permissions/*、migrations/sql/004_source_acl.sql、tests/test_source_acl.py、tests/test_permission_sync.py、tests/test_permission_e2e.py；修改 storage.py、config.py、cli.py、sync.py、connectors/*、graph/postgres.py。

**Interface：** PermissionConnector.fetch_permissions(external_ids) -> PermissionBatch；PermissionSnapshot 具 permission_revision/state/public/grants/complete/verified_at；PermissionStore.apply(snapshot, observation_sequence) 使用 CAS。首个 Adapter 为 Feishu，filesystem 继续明确配置授权。

- [ ] 写 test_collection_and_source_grants_intersection、test_permission_only_does_not_load_models、test_delayed_response_cannot_restore_revoked_access、test_partial_acl_never_expands_access、test_old_binary_private_fail_closed、test_content_scan_delayed_acl_cannot_restore_permission_worker_revocation、test_source_acl_private_graph_admission；正文旧观察不得覆盖独立撤权，私有资料不得为通过图准入而改成公开。
- [ ] 运行 python -m pytest tests/test_source_acl.py tests/test_permission_sync.py tests/test_permission_e2e.py -q，确认先失败。
- [ ] 实现 §6.2、所有读/写路径共用的权威 ACL 规则和独立 worker，更新 observe/publish/touch_unchanged 与图任务准入；先兼容公开模式，再实现已核验的飞书用户/组 ACL。将支持文件类型及继承边界写入能力表。
- [ ] 同命令通过；验证 employee 停用、集合撤权、组解绑、TTL 过期、正文无变化、模型不可用、来源停用以及所有 read/search/graph/table 旁路。
- [ ] 单独验收真实飞书“内容未变但权限改变”，只操作经批准的测试资料和测试主体；未实接则标适配待验收。
- [ ] 更新来源权限与安全回滚文档，提交；不变更现有 A 的资料范围。

### T11：授权范围内的性能优化

**依赖：** T01、T02、T08、T10。**文件：** 修改 storage.py/retrieval.py；新增 scripts/benchmark_retrieval.py、tests/test_retrieval_authorization_invariance.py、必要索引迁移。

**Interface：** 原 RetrievalBackend.retrieve 不变；BenchmarkManifest 固定数据快照、身份、模型、top-k、预算；报告不含私有原文。

- [ ] 写 test_hidden_documents_do_not_change_visible_bm25、test_acl_change_invalidates_cached_statistics、test_new_postings_matches_legacy_ranking。
- [ ] 运行 python -m pytest tests/test_retrieval_authorization_invariance.py -q，确认针对新优化的断言先失败。
- [ ] 在隔离库采集执行计划，实施必要连接/词项索引和批量加载；统计缓存/ANN 仅在满足 §6.3 后添加，不能先选技术再牺牲授权语义。
- [ ] 同命令通过；运行脚本比较 1k/10k/100k 数据，固定可见资料并大幅增加隐藏资料，核对结果与资源曲线。
- [ ] 完成第二批独立审查和回滚演练后提交；报告性能实测，不把样本测试包装为百万级保证。

### T12：查询感知图检索与路径解释

**依赖：** T01、T04、T13。**文件：** 新增 graph/query.py、tests/test_query_graph.py、tests/test_query_graph_e2e.py；修改 graph/facts.py、graph/search.py、graph/postgres.py、graph/neo4j_store.py、service.py。

**Interface：** GraphQueryOptions；QueryGraphStore.neighbors_authorized(..., facts, limit)；PostgreSQL authorized_fact_candidates 在权限内筛选。读取 T13 已建立的事实/支持表，限定当前记录与当前有效支持；不再维护另一份当前事实权威表。

- [ ] 写 test_different_query_selects_different_relations、test_hidden_edges_cannot_consume_visible_budget、test_final_path_revoke_but_independent_hit_survives、test_no_entity_guessing。
- [ ] 运行 python -m pytest tests/test_query_graph.py tests/test_query_graph_e2e.py -q，确认先失败。
- [ ] 实现授权事实候选、本体/别名匹配、有限扩展及可选路径；不直接使用全图 PageRank，也不让隐藏节点参与排序。
- [ ] 同命令通过，真实 PG/Neo4j 检查循环、大邻接、两个 company 同名实体、quote 校验和图不可用降级。
- [ ] 固定关系问题集比较 seed/query_aware，提交；不达质量门槛时保持开关关闭而非假报收益。

### T13：双时间事实与冲突权威存储

**依赖：** T09、T10。**文件：** 新增 graph/facts.py、history/domain.py、history/postgres.py、migrations/sql/005_history.sql、tests/test_temporal_facts.py、tests/test_temporal_postgres_e2e.py；修改 graph/worker.py、sync.py。

**Interface：** TemporalQuery(valid_as_of, known_as_of, include_unknown_validity)；FactObservation、FactSupport、FactEvent；record_observations 在事务中写入事实/支持/事件/投影任务。

- [ ] 写 test_late_evidence_has_two_distinct_times、test_no_backdated_system_knowledge_on_migration、test_unknown_validity_excluded_by_default、test_multivalued_relation_not_conflict、test_hidden_conflict_not_exposed。
- [ ] 运行 python -m pytest tests/test_temporal_facts.py tests/test_temporal_postgres_e2e.py -q，确认先失败。
- [ ] 实现 §7.2 与原文版本元数据快照（含可核验 accepted_at），显式替代及独立支持；不覆盖原事实，不以资料上传时间填 valid_from。
- [ ] 同命令通过；迟到、未来生效、纠错、半开边界、UTC/+08:00 等价、重复事件、删除一源/全源测试通过。
- [ ] 记录历史可追溯起点和旧数据缺口，提交；不得宣称迁移后知道所有过去状态。

### T14：历史检索与当前权限验证

**依赖：** T13、T05、T10。**文件：** 新增 history/service.py、tests/test_history_api.py、tests/test_history_authorization.py；修改 api.py、mcp_server.py、harness_plugin.py、config.py。

**Interface：** HistoryReader 与 §3.2 两个工具；history_evidence 是独立验证路径，不放宽旧 read。

- [ ] 写 test_old_read_still_rejects_inactive_version、test_historical_read_uses_current_acl、test_known_time_and_valid_time_differ、test_missing_old_metadata_not_fabricated、test_version_accepted_after_known_time_rejected、test_history_search_read_share_known_time；缺接受时间不允许补用当前版本。
- [ ] 运行 python -m pytest tests/test_history_api.py tests/test_history_authorization.py -q，确认先失败。
- [ ] 实现 PG 历史检索、引用与分页，明确 unknown/conflict/retention 状态；可选历史图投影使用独立 key，非首版强制依赖。
- [ ] 同命令通过；跨版本请求、撤权/停用/删除/过期 TTL 后任何历史读取都拒绝；投影故障不拿当前结果冒充历史结果。
- [ ] 更新工具说明和示例，提交；停用功能不删除历史档案。

### T15：受控视觉资产与独立模型运行时

**依赖：** T06、T07、T09、T10、T13。**文件：** 新增 visual/domain.py、visual/encoder.py、visual/colqwen.py、visual/worker.py、visual/postgres.py、migrations/sql/006_visual.sql、tests/test_visual_runtime.py、tests/test_visual_assets.py、deployment/compose.visual.yaml；修改解析资产协议、config.py、cli.py。T13 提供已落地的 005 迁移，保证 006 顺序唯一。

**Interface：** VisualEncoder 输出 MultiVector；VisualAssetManifest 仅内部受控键/hash/尺寸/版本/定位；资源产生任务沿用 durable job 的租约和幂等原则。

- [ ] 写 test_visual_off_imports_no_heavy_dependency、test_arbitrary_path_and_external_url_rejected、test_image_bomb_and_hash_mismatch_rejected、test_vector_shape_and_model_fingerprint。
- [ ] 运行 python -m pytest tests/test_visual_runtime.py tests/test_visual_assets.py -q，确认先失败。
- [ ] 先实现 006 中资产、任务与向量存储 schema 及持久化基础，再按 §8 实现隔离模型/资产 worker；固定 processor 查询格式和权重版本，不改文本模型依赖、不自动联网下载。
- [ ] 同命令通过，另运行明确标记的真实 ColQwen2 推理夹具；保存模型/资源/依赖清单与结果。缺硬件或权重只能标待验收。
- [ ] 审核第三方及模型许可、资源限额和空闲 GPU 安排，提交；不占用无关服务。

### T16：视觉索引、搜索和授权资源读取

**依赖：** T15、T10、T11。**文件：** 新增 visual/service.py、tests/test_visual_search.py、tests/test_visual_resource_e2e.py；扩展 T15 已建立的 visual/postgres.py；修改 api.py、mcp_server.py、config.py。复用 006 schema，不在本任务重复创建迁移。

**Interface：** VisualRetrievalBackend 在授权资产范围内 coarse_candidates → MaxSim；返回 VisualHit，不伪装 SearchHit。read_asset 返回每次授权的字节流。

- [ ] 写 test_visual_candidate_scope_before_ranking、test_asset_revoked_before_download、test_visual_index_rebinds_version、test_visual_unavailable_does_not_break_text_search。
- [ ] 运行 python -m pytest tests/test_visual_search.py tests/test_visual_resource_e2e.py -q，确认先失败。
- [ ] 实现多向量存储、授权粗召回/精排、资产/表格定位、HTTP/MCP 资源读取；所有图片链接只可经认证，禁止公有裸路径。
- [ ] 同命令通过；真实模型＋PG 验证中文表格、图表、扫描页；分别统计检索、定位、数值保真和粗召回相对精确 MaxSim 的损失。
- [ ] 记录功能兼容矩阵及默认关闭开关，提交；MCP 客户端不支持图像资源时只明确告知，不绕过权限。

### T17：三批联调、发布材料和最终独立审查

**依赖：** T00–T16。**文件：** 新增 tests/test_knowledge_e2e_v2.py、docs/UPGRADE-KNOWLEDGE.md、docs/VALIDATION-THREE-PHASES.md；完善 README、示例配置、MCP 与 SuiteHarness 适配器文档。

**Interface：** 同一只读工具契约用于原生 HTTP、MCP 和 KnowSeam 的 SuiteHarness Adapter；不要求产品修改原有参数。

- [ ] 写端到端夹具：导入公开合成资料 → 目录 → 搜索 → 上下文/表格 → 单处更新 → 复用 → 撤权 → 图/历史/图片均不可读 → 恢复合法授权 → 对照旧工具。
- [ ] 运行 python -m pytest tests/test_knowledge_e2e_v2.py -q，确认先失败。
- [ ] 补齐跨模块问题、迁移/回填/回滚操作手册和能力矩阵，不添加测试专用生产旁路。
- [ ] 运行全量 pytest、Ruff 和 git diff --check；所有跳过项逐条分类，新增功能所需真实 PG/Neo4j/解析/视觉测试不能以 skip 代替通过。
- [ ] 固定调用方做一轮只读端到端问答验收，并验证 A 的旧检索/全文工具；考核发题/答题/抄送仅确定性替身测试，不真实发送消息。
- [ ] 独立审查代码、Schema、权限竞争、恢复与部署文档；缺陷闭环后产出完整验收记录。
- [ ] 经用户确认具体切换窗口后才执行第 12 节上线；GitHub 推送另需明确请求。

## 10. 依赖与并行安排

| 执行组 | 可并行 | 合并前置 |
| --- | --- | --- |
| 基础 | T00 后，T01 与 T02 | 统一 contracts、迁移 ID 和配置命名 |
| 第一批 | T03 目录 与 T05 上下文；T04 等 T03 | 第一批接口/质量回归通过 |
| 第二批 | T06→T07 表格，与 T08→T09 增量 | 统一发布事务；T10 统一所有读取的授权谓词 |
| 性能 | T11 在 ACL、postings 语义稳定后 | 不与权限结构重构同时调参 |
| 第三批 | T13 事实存储完成后 T12 图检索、T14 历史读取、T15→T16 视觉可并行 | 先 005 后 006；先建权威事实表，再接图策略；共享当前权限、证据与资源契约 |
| 收尾 | T17 | 每批验收、迁移、回滚均有证据 |

同一 storage.py、service.py、config.py、api.py、mcp_server.py 由主负责人统一整合，避免并行覆盖。数据库迁移顺序单一，不由多个任务各自占号。这里的并行指当前任务子工作，不是向其他用户窗口发消息。

## 11. 测试分层与完成门槛

### 11.1 四层验证

1. 纯单元/契约：分词、跨度、预算、Schema、版本、状态；模型可用确定性替身。
2. 真实存储：独立 PostgreSQL knowseam_test、独立 Neo4j、故障/并发/撤权；不能用内存替身代替数据库宣称集成通过。
3. 真实解析与推理：Docling、文本 embedding/reranker、图抽取与视觉模型分别核验；不要求每次回归付费调用，但发布验收至少有可核验记录。
4. 固定 Agent 端到端：保持模型/提示词/预算固定，区分检索缺证据与模型未用好证据；不把 LLM-as-judge 当唯一真值。

已有 SUITE_KNOWLEDGE_TEST_DSN 只允许 knowseam_test；真实 Neo4j 测试沿用 KNOWSEAM_TEST_NEO4J_URI 的本机测试端口限制。凭据通过安全测试配置注入，文档和命令输出不出现密码。测试实例与生产服务分离。

### 11.2 质量集

- 第一批冻结十类问题、来源快照、必要证据和替代证据。合成夹具与真实私有评测集分开保存。
- 每项增强做单项消融，再做组合；模型/语料/身份/候选/返回预算相同。
- 企业真实标注需要人工复核或有可核验规则；禁止模型自动生成金标又自行评分后宣称企业质量达标。
- 文档 Recall 与证据完整率分开，图片命中与数字读取分开，软件正确性与模型质量分开。
- 如无真实资料/模型验收条件，该部分明确标“待验收”，不能为了结束任务编造成果。

### 11.3 每批硬门槛

- 新增契约、权限负例、版本/删除竞争、幂等/恢复、迁移/回滚测试全部通过。
- 默认 legacy 请求与旧工具兼容，原产品 A 无需增加工具参数；B 不受数据库/服务变更影响。
- 任何未授权证据输出为发布阻断，不与平均质量分数折算。
- 第一批：目录枚举无漏页，邻块/父节来源准确，覆盖状态不虚报；证据评测可重放。
- 第二批：表格精确值/单位不丢失；单点变更只重算受影响计算产物，撤权不等模型；新旧索引结果可比较。
- 第三批：图路径引用可核验、双时间语义正确、视觉资源没有鉴权旁路；真实模型测试不能全跳过。
- 性能报告包含 P50/P95、峰值内存、索引/资产空间、外部调用量及超时率。默认路径不能因关闭的新能力额外调用模型。
- 新策略只有在目标问题集有可复现收益、普通事实/无答案切片无实质退化且资源预算可接受时才启用。阈值在基线冻结时写入 evaluation manifest，不能观察结果后偷偷改判定口径；本文件不虚构统一“提升百分之几”。

## 12. 迁移、上线与回滚

### 12.1 实施前

- [ ] 读取服务器和仓库指令、git status；记录已有修改和服务镜像/启动时间，不接管其他任务 SSH。
- [ ] 为当前任务建立独立 SSH PTY；私钥和凭据不写入源码、日志或报告。
- [ ] 明确专用测试库/端口/资源边界；不调整无关容器、驱动或网络。
- [ ] 生成迁移 dry-run 和受影响表/索引清单，估算锁、额外空间及回填时长。
- [ ] 生产任何写迁移前保存可恢复备份，并在隔离库做一次恢复验证；不能仅看到备份文件就当可回滚。
- [ ] 如空间不足、来源私有 ACL 不可验证或视觉资源未批准，停止对应上线动作并报告，其他开发可继续。

### 12.2 每批上线顺序

1. 保存代码/镜像/配置版本和数据库备份；记录 task-owned SSH session。
2. 执行新增 schema 迁移；不自动改已有授权，不切换 A/B。
3. 先部署兼容 v1/v2 的解析器和读端，再部署新写端；新开关保持关闭。
4. 受控执行回填/索引构建，检查版本、引用、权限及任务积压。
5. 验证原 knowledge_list/search/read，再按集合逐项启用新能力；每次只改变一个策略。
6. 观察错误率、P95、ACL TTL、撤权传播、任务租约与磁盘；验证真实查询不发送员工消息。
7. 输出“实现/测试/启用/未启用”清单。已有知识库容器需要重启时，只重启明确的知识模块，A/B 不随意连带重启。

### 12.3 回滚

- 目录、覆盖、上下文、表格、图策略、历史、视觉分别关闭；服务恢复 legacy；新表先保留，不执行 DROP。
- 解析器按先停止 v2 写入、再回退客户端/服务端的顺序，避免协议错配。
- 新词项通道回退到已保留的旧索引；不得回滚员工撤权和来源删除。
- source_acl 私有资料保持 public=false，回滚旧程序先隔离该来源；旧程序安全拒读优于扩大权限。
- 当前图和历史图使用各自投影标识；重建从 PostgreSQL 权威记录出发，不能恢复已过期权限。
- 回填中断根据检查点续跑，旧 worker 不允许覆盖新 revision；模型缓存损坏只导致重算，不导致错误证据生效。
- 出现业务数据损坏才按明确恢复计划处理；恢复数据库前必须重新对齐最新授权/删除记录，不能盲目还原整库。

## 13. 交付物与最终完成定义

- [ ] 三批代码及兼容配置示例、类型/HTTP/MCP Schema。
- [ ] 显式迁移、回填、状态检查、继续/停止及回滚命令。
- [ ] 可公开合成夹具与独立私有质量集格式。
- [ ] 每批测试记录，真实存储/解析/模型验收状态，未执行项清单。
- [ ] 能力矩阵：旧 HTTP/MCP、KnowSeam SuiteHarness Adapter、新资源工具及客户端兼容边界。
- [ ] 运维文档：数据量/资源/依赖/凭据位置说明，权限同步、历史保留、资产清理与故障排查。
- [ ] 第三方代码/模型许可证和锁定版本记录，不将企业内容带入 GitHub。
- [ ] 当前运行配置启用了什么、未启用什么；不能把“功能关闭”写成“未开发”，也不能把“代码存在”写成“已上线”。

最终完成要求：T00–T17 全部实现并按对应层级验收；无已知发布阻断问题。生产切换和 GitHub 发布按用户确认的范围单独记录。

## 14. 调研依据和术语

- EnterpriseRAG-Bench（2026）：企业完整性/冲突/无答案评测。[GitHub](https://github.com/onyx-dot-app/EnterpriseRAG-Bench)
- Sufficient Context（ICLR 2025）：区分检索材料不足与模型利用不足。[官方论文](https://research.google/pubs/sufficient-context-a-new-lens-on-retrieval-augmented-generation-systems/)
- Docling：结构感知和 tokenizer 长度预算。[官方说明](https://docling-project.github.io/docling/concepts/chunking/)
- Haystack：按命中扩展上下文窗口。[源码](https://github.com/deepset-ai/haystack/blob/v3.3.0/haystack/components/retrievers/sentence_window_retriever.py)
- LightRAG：来源依赖与共同事实重建。[源码](https://github.com/HKUDS/LightRAG/blob/v1.5.7/lightrag/lightrag.py)
- GraphRAG-Bench（ICLR 2026）：按任务评估是否需要图。[论文](https://arxiv.org/abs/2506.05690v3)
- Graphiti：事实有效时间与系统记录时间。[源码](https://github.com/getzep/graphiti/blob/v0.30.2/graphiti_core/edges.py)
- TaR-ViR（ICLR 2026）：表格视觉检索，不代表数值已读对。[论文](https://proceedings.iclr.cc/paper_files/paper/2026/hash/7422317f84e8c83e4c1ad2ff87e1e88e-Abstract-Conference.html)
- ColQwen2 模型卡与 ColPali 实现：本轮核查多向量、依赖隔离及训练语言边界；未下载或执行模型。[模型卡](https://huggingface.co/vidore/colqwen2-v1.0) · [实现](https://github.com/illuin-tech/colpali)
- Onyx：只借鉴来源权限同步机制；EE 许可不得当 MIT 复制。[权限模型](https://github.com/onyx-dot-app/onyx/blob/main/backend/onyx/access/models.py) · [许可证](https://github.com/onyx-dot-app/onyx/blob/main/LICENSE)

术语：chunk=资料片段；reranker=重排序模型；RRF=按各通道排名融合；ACL=访问控制；outbox=与数据同事务写入的待处理事件；checkpoint=断点记录；fencing=旧任务不能覆盖新任务的序号保护；Adapter=接口的具体适配实现；as_of=查询指定时点；MaxSim=多向量逐项最大匹配后汇总。技术名称解释以本文具体契约为准，不把论文宣传数字当作 KnowSeam 的已验证收益。
