# 可选知识读取能力

本文件说明三批增强实现的接口；功能启用、数据库迁移和部署验收是不同步骤。所有 `features` 默认关闭，不会在服务启动时迁移、回填数据或下载模型。

## 第一批

- `features.context`：持久化原始父节及块到原文的字符区间。`POST /v1/context` 使用已检索的 `document_id/version_id/chunk_ids`，读取邻块或原始父节。旧索引的邻块会标明降级；不能把拼接的检索文本冒充父节。
- `features.catalog`：从私有配置 `catalog.entries` 登记目录项，字段为 `source_id/external_id/kind/item_id/name/aliases/metadata`。同一条目可由多个来源文档佐证；企业词典不会自动转为目录。配置 `catalog.scopes` 声明来源范围。只有扫描完成才报告该范围完整。
- `POST /v1/catalog`：`collection_ids/kind/cursor/limit`；limit 默认 50、上限 100。游标绑定身份和可见快照，十分钟过期；撤权、版本更新或服务重启后可能需重新枚举。无目录返回 unavailable，不生成猜测清单。
- `features.coverage`：搜索增加 `strategy=coverage`，先对有权限的有界候选重排，再每文档最多保留 `coverage.max_per_document` 个块（默认 2）并去除高度重叠区间。原 `legacy` 默认行为不变，最终 limit 仍为 1–20。
- `filters` 只接受 `kind/topic/document_type`，值必须在 `coverage.allowed_filters` 中登记。过滤依据为已授权目录的来源证据与配置元数据，不执行自由 SQL。过滤作用于既有候选池，可能少召回；完整列举应使用目录。
- `coverage.complete=null` 表示普通语义检索无法判断完整性；仅显式指定已核验目录范围时可返回 true/false。不会通过填充不相关资料使数字好看。
- `GET /v1/capabilities`：当前身份可用的可选适配器。未启用能力返回 `capability_unavailable`，不是“搜索无结果”。

## 第二批

- `features.tables`：按文档版本列出表格、选择行列读取类型化单元格；保留原文、精确数值字符串、公式缓存及合并锚点，不执行公式。
- `indexing.enabled`：词项不可变计算产物可按内容与词典指纹复用，证据定位和授权仍逐版本绑定。默认 shadow 对照模式，reuse 回退须先物化旧索引。
- 图抽取缓存：按模型版本、提示、本体、原文指纹复用，提交前重新校验引文和当前任务租约；不复用旧版本证据身份。
- `permissions.enabled`：来源权限与集合授权取交集；独立权限刷新不等待模型。通用接口已实现，飞书私有用户/群组/继承 ACL 的完整适配仍未完成，无法核实时拒绝读取。

## 第三批

- `features.query_graph`：`strategy=relationship` 使用有权事实和本体展开有限路径，可请求路径引文，不开放任意 Cypher。
- `features.history`：区分原文明示的有效时间与系统接受时间；历史搜索、原文读取仍按当前权限。快速更新遗漏事实抽取时明确返回 partial，不宣称历史全量。
- `features.visual`：独立离线运行时、受控图片/PDF 页图、授权粗召回加 MaxSim、多次校验的 HTTP/MCP 图片资源。当前协议与存储已测试，真实视觉模型和中文质量尚未验收，默认关闭。
- 所有可选模型/数据库都不属于业务产品逻辑；缺可选组件不影响旧文本工具。能力就绪状态不等于业务效果达标。

参数、传输和降级边界见 [检索契约](RETRIEVAL-CONTRACT.md)，部署回退见 [升级指南](UPGRADE-KNOWLEDGE.md)，实际结果见 [三批验收记录](VALIDATION-THREE-PHASES.md)。

## 可观测与安全

`observability.enabled` 控制去正文的阶段日志，默认关闭。日志仅包含服务生成的 request_id、阶段耗时、返回数量、预算与固定降级枚举；不写查询、正文、身份、令牌或 URL。独立有界日志队列故障或阻塞时丢弃诊断，不能拖垮查询。

`include_diagnostics=true` 只是请求，仍须在私有 `observability.diagnostic_actor_ids` 中授权。普通用户不能自行开启。搜索与原文读取均重新验证当前版本和 ACL；旧版本不会从普通 read/context 接口绕过。

## 客户端兼容

MCP Gateway 与 KnowSeam 的 SuiteHarness 插件使用 `extended_tools=true` 注册新增工具；默认为 false，继续暴露原三个工具。无需更改 SuiteHarness 核心或产品 A/B。MCP 使用 `knowledge_catalog/knowledge_context/knowledge_capabilities`；SuiteHarness 使用对应 `knowledge.*` 名称。身份来自可信凭据或运行时作用域，不存在工具参数 `actor_id`。

## 迁移与验证

先在独立测试库运行 `knowseam --config <private-config> migrate --dry-run`，审核后显式 `--apply`。具体 CLI 参数以 `--help` 为准。迁移有连续编号、校验和和互斥锁，已应用 SQL 不可改写。功能开关关闭后原检索仍可使用；不要删除生产旧版本作为回滚步骤。

评测报告区分合成契约验证与真实模型质量；源码测试通过不代表私有质量集、生产规模或真实 Docling 模型已通过。
